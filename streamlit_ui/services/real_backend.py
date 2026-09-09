"""Real-backend adapters: wire the actual agent persistence into the Streamlit UI.

This module contains the "real" implementations of the UI's three protocols,
built on ``agent.report_agent.AgentRuntime``:

- ``RealCheckpointerAdapter`` → LangGraph async checkpointer (SQLite/Postgres)
- ``RealFileStoreAdapter``    → LangGraph async store (the virtual filesystem)
- ``RealAgentRunnerAdapter``  → the real LangGraph agent, streaming events

## Modes

- **Partial swap** (``STREAMLIT_USE_REAL_BACKEND=1``): real checkpointer + store,
  mock agent (no LLM / no API keys).
- **Full agent** (``STREAMLIT_USE_REAL_AGENT=1``): real checkpointer + store +
  real ``runtime.agent`` (needs model keys in ``.env``).
- Production default (no env): full real integration.

## Why a dedicated event loop thread

``AgentRuntime`` opens aiosqlite connections (and/or an async Postgres pool).
Those bind to the event loop they were created on, but Streamlit runs synchronous
scripts. So every async call runs on one persistent background loop, driven from
the sync UI thread via ``asyncio.run_coroutine_threadsafe`` (synchronous
counterpart of ``EventLoopThread.run`` for streaming via a queue).

## Namespacing (why the UI checkpointer uses ``checkpoint_ns="ui"``)

The real agent persists its conversation history in LangGraph checkpoints under
``checkpoint_ns=""``. If the UI wrote its display history into that same
namespace it would clobber the agent's state, wiping its memory. In full mode the
UI therefore keeps its *display* history in a separate namespace (``ui``) using a
**fixed checkpoint id**, so re-saving replaces the UI row in place without ever
touching the agent's rows. ``list_threads`` scans all namespaces and
``delete`` removes the whole thread.

## File store format

Files live in namespace ``("filesystem",)`` as v2 values, the exact format the
real agent's ``FilesystemMiddleware`` (deepagents ``StoreBackend``) writes:
``{"content": <str>, "encoding": "utf-8"}``. Keys are paths like
``findings/nmap.txt`` / ``reports/report.md``, so files the agent later writes
appear in the UI and vice versa.

## Debug printing of agent output

When ``STREAMLIT_AGENT_PRINT=1`` the real agent runner prints each streamed tool
call / tool result / text fragment to stdout (like the acceptance tests do), so
a developer can observe what the LLM is outputting while driving the UI.
"""

import asyncio
import logging
import os
import queue
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from streamlit_ui.services.agent_service import PenTestAgentService

_NAMESPACE = ("filesystem",)
_UI_CHECKPOINT_NS = "ui"

_TRUE_MODES = ("1", "true", "yes")

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------

def _env_enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in _TRUE_MODES


def is_real_backend_mode() -> bool:
    """True when the app uses real persistence (partial swap, or full agent)."""
    return _env_enabled("STREAMLIT_USE_REAL_BACKEND")


def is_real_agent_mode() -> bool:
    """True when the app uses the real agent (implies real persistence)."""
    return _env_enabled("STREAMLIT_USE_REAL_AGENT")


def is_agent_output_print_enabled() -> bool:
    """True when the real agent runner should print streamed output to stdout."""
    return _env_enabled("STREAMLIT_AGENT_PRINT")


def backend_label(runtime: Any) -> str:
    """Human-readable backend description for the debug status report.

    Args:
        runtime: An initialized ``AgentRuntime`` instance.

    Returns:
        ``"postgres: <masked uri>"`` when a pool is live, else
        ``"sqlite: <path>"``.
    """
    from agent import report_agent as ra

    if runtime.pool is not None:
        uri = (ra.DB_URI or "unknown").split("?", 1)[0]
        return f"postgres: {uri}"
    return f"sqlite: {ra.LOCAL_DB_PATH}"


# ---------------------------------------------------------------------------
# Event loop; the bridge between Streamlit's sync thread and async LangGraph
# ---------------------------------------------------------------------------

class EventLoopThread:
    """Persistent asyncio loop running on a daemon thread.

    All real LangGraph async operations (checkpointer + store + agent streaming)
    are scheduled on this loop so the aiosqlite connections / Postgres pool stay
    attached to a single loop.
    """

    def __init__(self) -> None:
        if sys.platform == "win32":
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._ready = threading.Event()
        self._thread = threading.Thread(
            target=self._thread_main,
            name="streamlit-ui-async",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=10.0):
            raise RuntimeError("Event loop thread failed to start")

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._ready.set()
        self._loop.run_forever()

    def run(self, coro: Any, timeout: float = 60.0) -> Any:
        """Run an awaitable on the persistent loop and block for its result."""
        if self._loop is None:  # pragma: no cover - defensive
            raise RuntimeError("Event loop not ready")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)

    def schedule(self, coro: Any) -> Any:
        """Schedule an awaitable on the persistent loop and return immediately."""
        if self._loop is None:  # pragma: no cover - defensive
            raise RuntimeError("Event loop not ready")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)


# ---------------------------------------------------------------------------
# Checkpointer adapter (real LangGraph saver)
# ---------------------------------------------------------------------------

class RealCheckpointerAdapter:
    """Adapt a real LangGraph async checkpointer to ``CheckpointerProtocol``.

    Session state is persisted as a real LangGraph checkpoint whose
    ``channel_values["messages"]`` holds the UI conversation. A **fixed**
    checkpoint id (``ui``) is used so re-saving replaces the row in place
    instead of piling up versions, and the optional ``checkpoint_ns`` keeps UI
    display data out of the agent's namespace (see module docstring).
    ``list_threads`` scans all checkpoint namespaces and de-dupes thread ids;
    ``delete`` removes the whole thread.
    """

    def __init__(self, runner: EventLoopThread, saver: Any, checkpoint_ns: str = "") -> None:
        self._runner = runner
        self._saver = saver
        self._checkpoint_ns = checkpoint_ns

    def _config(self, thread_id: str) -> Dict[str, Any]:
        return {
            "configurable": {
                "thread_id": thread_id,
                "checkpoint_ns": self._checkpoint_ns,
            }
        }

    def get(self, thread_id: str) -> Optional[Dict[str, Any]]:
        async def _get() -> Optional[Dict[str, Any]]:
            tup = await self._saver.aget_tuple(self._config(thread_id))
            if tup is None:
                return None
            messages = tup.checkpoint.get("channel_values", {}).get("messages", [])
            return {"messages": messages}

        return self._runner.run(_get())

    def put(self, thread_id: str, state: Dict[str, Any]) -> None:
        messages = list(state.get("messages", []))
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        checkpoint = {
            "v": 1,
            "ts": ts,
            "id": "ui",
            "channel_values": {"messages": messages},
            "channel_versions": {"messages": 1},
            "versions_seen": {},
            "pending_sends": [],
        }

        async def _put() -> None:
            # Fixed id -> upsert in place. Never deletes the thread, so the
            # agent's own checkpoints (different namespace) survive.
            await self._saver.aput(
                self._config(thread_id),
                checkpoint,
                {"source": "ui"},
                new_versions={"messages": 1},
            )

        self._runner.run(_put())

    def list_threads(self) -> List[str]:
        async def _scan() -> List[str]:
            thread_ids = set()
            async for tup in self._saver.alist(None):
                tid = tup.config["configurable"]["thread_id"]
                thread_ids.add(tid)
            return sorted(thread_ids)

        return self._runner.run(_scan())

    def delete(self, thread_id: str) -> bool:
        async def _delete() -> bool:
            existing = await self._saver.aget_tuple(self._config(thread_id))
            if existing is None:
                return False
            # adelete_thread removes every checkpoint of the thread (all ns),
            # i.e. UI history AND the real agent's LangGraph state.
            await self._saver.adelete_thread(thread_id)
            return True

        return self._runner.run(_delete())


# ---------------------------------------------------------------------------
# File store adapter (real LangGraph store)
# ---------------------------------------------------------------------------

class RealFileStoreAdapter:
    """Adapt a real LangGraph async store to ``FileStoreProtocol``.

    Files are stored under namespace ``("filesystem",)`` in the same v2 shape
    the agent's FilesystemMiddleware uses, so the UI and the agent share one
    virtual filesystem.
    """

    def __init__(self, runner: EventLoopThread, store: Any) -> None:
        self._runner = runner
        self._store = store

    @staticmethod
    def _to_text(content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, (bytes, bytearray)):
            return bytes(content).decode("utf-8", errors="replace")
        if isinstance(content, list):  # legacy v1 list[str] format
            return "\n".join(content)
        return str(content)

    def read_file(self, path: str) -> Optional[bytes]:
        item = self._runner.run(self._store.aget(_NAMESPACE, path))
        if item is None:
            return None
        value = item.value or {}
        text = self._to_text(value.get("content", ""))
        return text.encode("utf-8")

    def write_file(self, path: str, content: bytes) -> None:
        value = {"content": self._to_text(content), "encoding": "utf-8"}

        async def _write() -> None:
            await self._store.aput(_NAMESPACE, path, value)

        self._runner.run(_write())

    def list_files(self, prefix: str = "") -> List[str]:
        items = self._runner.run(self._store.asearch(_NAMESPACE, limit=10000))
        return sorted(item.key for item in items if item.key.startswith(prefix))

    def delete_file(self, path: str) -> bool:
        async def _delete() -> bool:
            existing = await self._store.aget(_NAMESPACE, path)
            if existing is None:
                return False
            await self._store.adelete(_NAMESPACE, path)
            return True

        return self._runner.run(_delete())


# ---------------------------------------------------------------------------
# Agent runner adapter (the real LangGraph agent)
# ---------------------------------------------------------------------------

class RealAgentRunnerAdapter:
    """Adapt ``runtime.agent`` to ``AgentRunnerProtocol``.

    Streams ``agent.astream(...)`` over the persistent event loop into a queue
    that the synchronous ``run()`` generator drains, so the UI sees tool calls
    arrive live and the final answer text after the run completes.

    The agent persists its own LangChain message history in the real
    checkpointer (namespace ``""``) keyed by ``thread_id``.

    When ``STREAMLIT_AGENT_PRINT=1`` every streamed event is also printed to
    stdout so a developer can watch exactly what the LLM is outputting while the
    UI runs - mirroring the acceptance-test loop of 3_a2a_testing.
    """

    def __init__(self, runner: EventLoopThread, agent: Any) -> None:
        self._runner = runner
        self._agent = agent

    def run(self, message: str, thread_id: str) -> Iterator[Dict[str, Any]]:
        from langchain_core.messages import AIMessageChunk, ToolMessage

        emit_print = is_agent_output_print_enabled()
        events: "queue.Queue[Any]" = queue.Queue()
        sentinel = object()
        final_parts: List[str] = []
        config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
        stream_input = {"messages": [{"role": "user", "content": message}]}

        def _print(*parts: Any) -> None:
            if emit_print:
                print(*parts, flush=True)

        def _handle_chunk(chunk: Any) -> None:
            if isinstance(chunk, ToolMessage):
                name = getattr(chunk, "name", "") or ""
                output = getattr(chunk, "content", "")
                events.put({"type": "tool_result", "data": {"name": name, "output": output}})
                preview = str(output)[:300]
                _print(f"[TOOL RESULT]: {name} -> {preview}")
                return

            if isinstance(chunk, AIMessageChunk):
                for tc in getattr(chunk, "tool_calls", []) or []:
                    data = {"name": tc.get("name"), "args": tc.get("args", {})}
                    events.put({"type": "tool_call", "data": data})
                    _print(f"[TOOL CALL]: {data['name']} args={data['args']}")

                content = getattr(chunk, "content", "")
                if content:
                    text = content if isinstance(content, str) else str(content)
                    if text.strip():
                        final_parts.append(text)
                        _print(f"[CNK]: {text}")

        async def _consume() -> None:
            try:
                async for update in self._agent.astream(stream_input, config=config):
                    chunks = update.get("messages", []) if isinstance(update, dict) else []
                    for chunk in chunks:
                        _handle_chunk(chunk)
            except Exception as exc:  # noqa: BLE001 - surface to UI consumer
                events.put({"type": "error", "data": f"{type(exc).__name__}: {exc}"})
                _print(f"[AGENT ERROR]: {type(exc).__name__}: {exc}")
            finally:
                if final_parts:
                    text = "".join(final_parts)
                    events.put({"type": "message", "data": text})
                    _print(f"[AGENT RESPONSE]: {text}")
                events.put(sentinel)

        self._runner.schedule(_consume())

        while True:
            item = events.get()
            if item is sentinel:
                break
            yield item


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: Optional[Tuple[str, EventLoopThread, Any]] = None


def create_real_backend(
    db_path: Optional[str] = None,
    timeout: float = 120.0,
    full: bool = False,
) -> Tuple[EventLoopThread, Any]:
    """Create a fresh runtime + event loop.

    Partial (``full=False``): persistence + observability only, no model keys
    required (``AgentRuntime.initialize_persistence``).
    Full (``full=True``): complete ``AgentRuntime.initialize`` - LLM, middleware
    and the agent too (needs model keys in ``.env``).

    Args:
        db_path: Override the SQLite file. Falls back to ``REPORT_AGENT_DB_PATH``
                 env, then the agent default (``agent/agent_session.db``).
                 Postgres is still used when ``POSTGRES_URI`` is reachable.
        timeout: Max seconds to block on initialization.
        full: Whether to build the real agent as well.

    Returns:
        ``(runner, runtime)``.
    """
    path = db_path or os.environ.get("REPORT_AGENT_DB_PATH")
    if path:
        from agent import report_agent as ra

        ra.LOCAL_DB_PATH = Path(path)

    from agent.report_agent import AgentRuntime

    runtime = AgentRuntime()
    runner = EventLoopThread()
    coro = runtime.initialize() if full else runtime.initialize_persistence()
    runner.run(coro, timeout=timeout)
    logger.debug("real backend initialized (%s): %s", "full" if full else "partial", backend_label(runtime))
    return runner, runtime


def get_real_backend(mode: Optional[str] = None) -> Tuple[EventLoopThread, Any]:
    """Return the process-wide real backend for the current mode.

    The backend is cached per mode (full vs partial) so the debug probe, the
    app, and every session share one event loop and one set of DB connections.

    Args:
        mode: ``"full"`` / ``"partial"``; defaults to the mode implied by
              ``STREAMLIT_USE_REAL_AGENT``.
    """
    global _CACHE
    if mode is None:
        mode = "full" if is_real_agent_mode() else "partial"
    if _CACHE is None or _CACHE[0] != mode:
        with _CACHE_LOCK:
            if _CACHE is None or _CACHE[0] != mode:
                _CACHE = (mode, *create_real_backend(full=(mode == "full")))
    return _CACHE[1], _CACHE[2]


def _reset_backend_cache() -> None:
    """Forget the cached backend (used by tests between modes)."""
    global _CACHE
    _CACHE = None


def build_real_backend_service(
    thread_id: str,
    force_full: bool = False,
) -> PenTestAgentService:
    """Build a service wired to the real backend.

    Partial (default): real checkpointer + store, mock agent - no API keys.
    Full (``force_full`` or ``STREAMLIT_USE_REAL_AGENT=1``): real agent too -
    the real LLM streams tool calls and replies into the chat.

    Args:
        thread_id: Session/thread identifier.
        force_full: Require the real agent (used by the production default).

    Returns:
        Configured ``PenTestAgentService``.
    """
    full = is_real_agent_mode() or force_full
    mode = "full" if full else "partial"
    runner, runtime = get_real_backend(mode=mode)
    if not full:
        from tests.mock.mock_objects_streamlit import MockAgentRunner

        agent_runner = MockAgentRunner(delay_seconds=0.35)
        agent_runner.set_tool_calls([
            {"name": "ls", "args": {"path": "/context/"}},
            {"name": "read_file", "args": {"path": "/context/interpretations/recon.md"}},
        ])
        return PenTestAgentService(
            thread_id=thread_id,
            checkpointer=RealCheckpointerAdapter(runner, runtime.checkpointer),
            file_store=RealFileStoreAdapter(runner, runtime.store),
            agent_runner=agent_runner,
        )

    if runtime.agent is None:
        raise RuntimeError(
            "Real agent is not available. Check your .env (MISTRAL_API_KEY) "
            "and that STREAMLIT_USE_REAL_AGENT=1 / production mode is active."
        )
    return PenTestAgentService(
        thread_id=thread_id,
        checkpointer=RealCheckpointerAdapter(runner, runtime.checkpointer, checkpoint_ns=_UI_CHECKPOINT_NS),
        file_store=RealFileStoreAdapter(runner, runtime.store),
        agent_runner=RealAgentRunnerAdapter(runner, runtime.agent),
    )


# ---------------------------------------------------------------------------
# Status report lines (used by runtime_status debug banner)
# ---------------------------------------------------------------------------

def component_status_lines() -> List[str]:
    """One plain line per component for the real backend (partial or full)."""
    from streamlit_ui.services import real_backend as _self

    full = is_real_agent_mode()
    try:
        runner, runtime = _self.get_real_backend()
    except Exception as exc:  # pragma: no cover - defensive
        reason = f"initialization failed: {exc}"
        return [
            f"checkpointer: not connected ({reason})",
            f"virtual filesystem store: not connected ({reason})",
            f"llm api: not connected ({reason})",
            f"agent object: not connected ({reason})",
            f"observability (langfuse): not connected ({reason})",
        ]

    label = backend_label(runtime)
    checkpointer = RealCheckpointerAdapter(runner, runtime.checkpointer)
    store = RealFileStoreAdapter(runner, runtime.store)

    lines: List[str] = []

    try:
        checkpointer.list_threads()
        lines.append(f"checkpointer: connected ({label})")
    except Exception as exc:  # pragma: no cover - defensive
        lines.append(f"checkpointer: not connected (raised: {exc})")

    try:
        store.list_files()
        lines.append(f"virtual filesystem store: connected ({label})")
    except Exception as exc:  # pragma: no cover - defensive
        lines.append(f"virtual filesystem store: not connected (raised: {exc})")

    if full and runtime.llm is not None:
        candidates = [
            getattr(runtime.llm, "model_name", None),
            getattr(runtime.llm, "model", None),
            getattr(runtime.llm, "name", None),
        ]
        model = next((c for c in candidates if c), "?")
        lines.append(f"llm api: configured (model: {model})")
    else:
        lines.append("llm api: not connected (agent not wired - use STREAMLIT_USE_REAL_AGENT=1)")

    if full and runtime.agent is not None:
        lines.append("agent object: connected (real LangGraph agent)")
    else:
        lines.append("agent object: not connected (partial swap keeps the mock agent runner)")

    if runtime.langfuse_ok:
        lines.append("observability (langfuse): connected (langfuse handler attached)")
    else:
        lines.append("observability (langfuse): not connected (no langfuse credentials/offline)")

    return lines