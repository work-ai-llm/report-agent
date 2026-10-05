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
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from streamlit_ui.services.agent_service import PenTestAgentService
from streamlit_ui.services.run_registry import REGISTRY, RunState, spawn

_NAMESPACE = ("filesystem",)
_UI_CHECKPOINT_NS = "ui"

# How often the blocking run() facade checks for new events. Small enough to feel
# live, large enough not to spin a core.
_POLL_INTERVAL = 0.02

_TRUE_MODES = ("1", "true", "yes")

logger = logging.getLogger(__name__)


async def _noop() -> None:
    """Awaitable no-op, so persistence can be unconditional."""
    return None


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


def is_streaming_enabled() -> bool:
    """True when the UI should render live token deltas (streaming).

    Defaults to on. Disabled only by ``STREAMLIT_STREAM`` set to
    ``0`` / ``false`` / ``no`` (the real agent keeps emitting raw events
    either way - this only controls whether the chat shows them live).
    """
    return os.environ.get("STREAMLIT_STREAM", "1").strip().lower() not in ("0", "false", "no")


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

    async def _anext_blob_version(self, thread_id: str) -> int:
        """Async body of :meth:`_next_blob_version`.

        Split out so the loop thread can await it directly. Calling
        ``self._runner.run(...)`` from the loop thread would deadlock: ``run``
        blocks the calling thread on a future that only the loop can complete.
        """
        tup = await self._saver.aget_tuple(self._config(thread_id))
        if tup is not None and tup.checkpoint is not None:
            base = (tup.checkpoint.get("channel_versions") or {}).get("messages", 0)
            try:
                return int(base) + 1
            except (TypeError, ValueError):
                return 1
        return 1

    def _next_blob_version(self, thread_id: str) -> int:
        """Return the next ``messages`` blob version for a thread.

        Blob version numbers must be strictly increasing per
        ``(thread_id, checkpoint_ns, channel)``: the ``checkpoint_blobs``
        table's upsert uses ``ON CONFLICT DO NOTHING`` on that key, so a reused
        version would silently keep the OLD blob (the "messages disappear"
        bug).  Derive the next number from the CURRENT checkpoint's
        ``channel_versions`` so it stays correct across adapter instances (a
        per-instance counter would reset and collide).
        """
        return self._runner.run(self._anext_blob_version(thread_id))

    async def aput_state(self, thread_id: str, state: Dict[str, Any]) -> None:
        """Persist UI conversation state from inside the event loop.

        This is the loop-thread-safe twin of :meth:`put`. The agent stream now
        persists itself as events arrive (so an interrupted Streamlit script run
        cannot lose the turn), which means the write happens ON the loop thread.
        Going through :meth:`put` there would call ``EventLoopThread.run`` and
        block the loop on itself - a hard deadlock. Hence a separate awaitable
        entry point that talks to the saver directly.
        """
        messages = list(state.get("messages", []))
        ver = await self._anext_blob_version(thread_id)
        checkpoint = {
            "v": 1,
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
            "id": "ui",
            "channel_values": {"messages": messages},
            "channel_versions": {"messages": ver},
            "versions_seen": {},
            "pending_sends": [],
        }
        # Fixed id -> upsert in place. Never deletes the thread, so the agent's
        # own checkpoints (different namespace) survive.
        await self._saver.aput(
            self._config(thread_id),
            checkpoint,
            {"source": "ui"},
            new_versions={"messages": ver},
        )

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
        """Persist UI conversation state from the Streamlit script thread."""
        self._runner.run(self.aput_state(thread_id, state))

    def list_threads(self) -> List[str]:
        async def _scan() -> List[str]:
            thread_ids = set()
            # POSTGRES FIX: `alist(None)` without a filter is an *unbounded*
            # full-table scan over EVERY namespace and thread (the agent's own
            # loop/input checkpoints too), with no LIMIT. AsyncPostgresSaver
            # materializes + de-blobs every checkpoint row (per-row blob fetches
            # and `asyncio.to_thread`), which on a busy Postgres DB exceeds the
            # EventLoopThread.run() 60s default and crashes the app at boot
            # (app.py list_threads on the placeholder service).
            # Only the UI writes metadata {"source": "ui"} (see put()), so scope
            # the scan to UI sessions; LIMIT keeps it bounded regardless of
            # backend. Works on SQLite (json_extract) and Postgres (metadata @>).
            # NOTE(perf, parked): on Postgres this `metadata @>` filter is a
            # full-table jsonb seq-scan (~0.5-0.6s per call over Tailscale, ~13s
            # on a cold pool). A GIN index on the JSONB column turns it into an
            # index scan:
            #   CREATE INDEX IF NOT EXISTS idx_checkpoints_metadata_gin
            #     ON checkpoints USING GIN (metadata);
            # This adds a SEPARATE index only - the checkpointer's table/schema
            # stays untouched, no column added, row format unchanged, fully
            # reversible (DROP INDEX). Deliberately not applied yet (parked in
            # the acceptance-test plan); revisit before scaling session counts.
            async for tup in self._saver.alist(None, filter={"source": "ui"}, limit=1000):
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

    @staticmethod
    def _norm(path: str) -> str:
        """Strip a leading ``/`` for display/prefix purposes only."""
        return path.lstrip("/")

    @staticmethod
    def _key(path: str) -> str:
        """Canonical store key (slash-prefixed, matching the agent's StoreBackend).

        Files routed through ``/context/`` land in the store keyed WITH a
        leading slash (``/findings/nmap.txt``).  The UI used to strip it, so
        UI uploads were invisible to the agent and agent-written reports were
        listed in the selector but unreadable.  Always write the slash form;
        reads try slash first with a legacy no-slash fallback so old rows
        still resolve.
        """
        return "/" + path.lstrip("/")

    def read_file(self, path: str) -> Optional[bytes]:
        item = self._runner.run(self._store.aget(_NAMESPACE, self._key(path)))
        if item is None:
            item = self._runner.run(self._store.aget(_NAMESPACE, self._norm(path)))
        if item is None:
            return None
        value = item.value or {}
        text = self._to_text(value.get("content", ""))
        return text.encode("utf-8")

    def write_file(self, path: str, content: bytes) -> None:
        value = {"content": self._to_text(content), "encoding": "utf-8"}

        async def _write() -> None:
            await self._store.aput(_NAMESPACE, self._key(path), value)

        self._runner.run(_write())

    def list_files(self, prefix: str = "") -> List[str]:
        items = self._runner.run(self._store.asearch(_NAMESPACE, limit=10000))
        prefix = self._norm(prefix)
        return sorted(
            self._norm(item.key)
            for item in items
            if self._norm(item.key).startswith(prefix)
        )

    def delete_file(self, path: str) -> bool:
        async def _delete() -> bool:
            key = self._key(path)
            existing = await self._store.aget(_NAMESPACE, key)
            if existing is not None:
                await self._store.adelete(_NAMESPACE, key)
                return True
            legacy = self._norm(path)
            if legacy != key:
                existing = await self._store.aget(_NAMESPACE, legacy)
                if existing is not None:
                    await self._store.adelete(_NAMESPACE, legacy)
                    return True
            return False

        return self._runner.run(_delete())


# ---------------------------------------------------------------------------
# Agent runner adapter (the real LangGraph agent)
# ---------------------------------------------------------------------------

def _copy_rows(rows: List[Any]) -> List[Any]:
    """Shallow-copy a message list, copying each dict row.

    Used when seeding a run from persisted history. An in-memory checkpointer
    hands back its stored list by reference; sharing those row objects would let
    the run's in-place annotation (``result=`` on a tool_call) mutate persisted
    state without a write.
    """
    return [dict(r) if isinstance(r, dict) else r for r in rows]


class RealAgentRunnerAdapter:
    """Adapt ``runtime.agent`` to ``AgentRunnerProtocol``.

    Streams ``agent.astream(...)`` on the persistent event loop, recording every
    event into a :class:`~streamlit_ui.services.run_registry.RunState` and
    persisting the UI-namespace conversation as it goes.

    ## Why the run owns its own persistence

    The agent stream is scheduled on ``EventLoopThread`` and is NOT tied to the
    Streamlit script run that started it. That is deliberate - it is what makes a
    turn survive the user clicking a sidebar widget mid-stream. But it means the
    script thread cannot be the thing that saves progress: a queued rerun unwinds
    the script without warning, so anything the script had not yet written is
    lost from the UI even though the agent keeps working.

    So the write happens here, on the loop thread, immediately after each event.
    The script thread becomes a pure reader. Losing the script run now costs at
    most the *rendering* of an event, never the event itself.

    ## Message shapes persisted here

    These are the same shapes ``app.py`` renders, so the persisted row is a
    faithful transcript and can be re-read on a later rerun:

    - ``{"type": "user", "content": str}``
    - ``{"type": "tool_call", "name": str, "args": dict, "result": str}``
    - ``{"type": "assistant", "content": str}``
    """

    def __init__(self, runner: EventLoopThread, agent: Any, checkpointer: Any = None) -> None:
        self._runner = runner
        self._agent = agent
        # Optional: without it the run is still tracked in the registry (so the
        # UI can follow progress) but nothing is persisted. Tests construct the
        # adapter with two args and assert event flow only.
        self._checkpointer = checkpointer

    # -- public API ---------------------------------------------------------

    def supports_polling(self) -> bool:
        """Always True: the registry path works with or without a checkpointer."""
        return True

    def start(self, message: str, thread_id: str) -> RunState:
        """Begin a turn and return immediately. Never blocks on the agent.

        The user message is persisted *before* the stream is scheduled, so a turn
        is durable from the moment it exists rather than from its first event.

        Returns:
            The registered :class:`RunState`; poll it for events.
        """
        existing: List[Dict[str, Any]] = []
        if self._checkpointer is not None:
            stored = self._checkpointer.get(thread_id) or {}
            existing = _copy_rows(stored.get("messages", []))

        state = REGISTRY.start(
            thread_id,
            message,
            existing + [{"type": "user", "content": message}],
        )
        # Durable immediately. Synchronous here is safe: we are on the script
        # thread, not the loop, so the blocking bridge cannot deadlock.
        if self._checkpointer is not None:
            self._checkpointer.put(thread_id, {"messages": list(state.messages)})

        self._runner.schedule(self._consume(state, message, thread_id))
        return state

    def poll(self, thread_id: str, index: int) -> Tuple[List[Dict[str, Any]], int, int]:
        """Return ``(events, next_index, revision)`` for incremental rendering."""
        state = REGISTRY.get(thread_id)
        if state is None:
            return [], index, 0
        events, next_index = state.events_since(index)
        return events, next_index, state.revision

    def run_revision(self) -> Optional[int]:
        """Change counter for this adapter's thread, or ``None`` if unknown."""
        return None

    def active_run(self, thread_id: str) -> Optional[RunState]:
        return REGISTRY.get(thread_id)

    def is_running(self, thread_id: str) -> bool:
        return REGISTRY.is_running(thread_id)

    def run(self, message: str, thread_id: str) -> Iterator[Dict[str, Any]]:
        """Blocking facade over :meth:`start` + :meth:`poll`.

        Yields exactly the events the agent produced, in order, then returns. Used
        by the UI's live-render loop and by tests. Prefer :meth:`start` when the
        caller must not be tied to the lifetime of one script run.
        """
        state = self.start(message, thread_id)
        index = 0
        while True:
            events, index, _rev = self.poll(thread_id, index)
            for event in events:
                yield event
            if not state.is_running:
                # Drain anything appended between the poll and the status check.
                events, index, _rev = self.poll(thread_id, index)
                for event in events:
                    yield event
                return
            time.sleep(_POLL_INTERVAL)

    # -- internals ----------------------------------------------------------

    def _persist(self, state: RunState, thread_id: str) -> Any:
        """Persist the run's message list. Returns an awaitable.

        Called on the loop thread, so it MUST NOT go through
        ``RealCheckpointerAdapter.put`` (which blocks on the bridge and would
        deadlock the loop against itself).
        """
        if self._checkpointer is None:
            return _noop()
        return self._checkpointer.aput_state(thread_id, {"messages": list(state.messages)})

    def _build_consumer(self, state: RunState, message: str, thread_id: str) -> Any:
        from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

        emit_print = is_agent_output_print_enabled()
        trace_path = os.environ.get("STREAMLIT_AGENT_LOG")
        final_parts: List[str] = []
        # Set when the consumer abandons this run, so the finally block can tell
        # "stopped by the user" apart from "finished".
        cancelled = False
        config = {
            "configurable": {"thread_id": thread_id, "checkpoint_ns": ""},
            # Langfuse reads this at the chain root and propagates it to every
            # child observation, so all turns of one UI thread collapse into a
            # single Langfuse session instead of one trace per model call.
            "metadata": {"langfuse_session_id": thread_id},
        }
        stream_input = {"messages": [{"role": "user", "content": message}]}

        def _print(*parts: Any) -> None:
            # Mirrored to STREAMLIT_AGENT_LOG (if set) so a run that is frozen
            # or killed under pytest capture still leaves a visible trace.
            line = " ".join(str(p) for p in parts)
            if emit_print:
                try:
                    print(line, flush=True)
                except UnicodeEncodeError:  # cp1252 consoles choke on unicode arrows etc.
                    # Best-effort console print - the trace file (utf-8) still
                    # preserves the exact bytes. Never crash the agent stream.
                    try:
                        safe = line.encode("utf-8", "replace").decode("ascii", "backslashreplace")
                        print(safe, flush=True)
                    except Exception:
                        pass
            if trace_path:
                try:
                    with open(trace_path, "a", encoding="utf-8") as fh:
                        fh.write(line + "\n")
                except OSError:  # pragma: no cover - logging must never break the run
                    pass

        def _emit(event: Dict[str, Any], additions: Optional[List[Dict[str, Any]]] = None) -> None:
            state.record_event(event, additions)

        def _attach_result(rows: List[Dict[str, Any]], output: str) -> List[Dict[str, Any]]:
            """Attach a tool result to the oldest unanswered tool_call (FIFO).

            Mirrors what ``app.py`` did when the script thread owned the list, so
            the persisted transcript keeps args + results the same way.
            """
            for row in rows:
                if row.get("type") == "tool_call" and "result" not in row:
                    row["result"] = output
                    break
            return []

        def _handle_chunk(chunk: Any) -> None:
            if isinstance(chunk, dict):
                _handle_dict_chunk(chunk)
                return
            if isinstance(chunk, ToolMessage):
                name = getattr(chunk, "name", "") or ""
                output = getattr(chunk, "content", "")
                _emit(
                    {"type": "tool_result", "data": {"name": name, "output": output}},
                    _attach_result(state.messages, str(output)),
                )
                preview = str(output)[:300]
                _print(f"[TOOL RESULT]: {name} -> {preview}")
                return

            if isinstance(chunk, (AIMessage, AIMessageChunk)):
                for tc in getattr(chunk, "tool_calls", []) or []:
                    data = {"name": tc.get("name"), "args": tc.get("args", {})}
                    _emit(
                        {"type": "tool_call", "data": data},
                        [{"type": "tool_call", "name": data["name"], "args": data["args"]}],
                    )
                    _print(f"[TOOL CALL]: {data['name']} args={data['args']}")

                content = getattr(chunk, "content", "")
                if content:
                    text = content if isinstance(content, str) else str(content)
                    if text.strip():
                        final_parts.append(text)
                        _print(f"[CNK]: {text}")

        def _handle_dict_chunk(chunk: Dict[str, Any]) -> None:
            # Serialized message form (some graph updates yield dicts).
            if chunk.get("tool_calls"):
                for tc in chunk["tool_calls"]:
                    data = {"name": tc.get("name") or tc.get("function", {}).get("name"),
                            "args": tc.get("args") or tc.get("function", {}).get("arguments", {})}
                    _emit(
                        {"type": "tool_call", "data": data},
                        [{"type": "tool_call", "name": data["name"], "args": data["args"]}],
                    )
                    _print(f"[TOOL CALL]: {data['name']} args={data['args']}")
            if chunk.get("role") == "tool":
                name = chunk.get("name") or ""
                output = chunk.get("content", "")
                _emit(
                    {"type": "tool_result", "data": {"name": name, "output": output}},
                    _attach_result(state.messages, str(output)),
                )
                preview = str(chunk.get("content", ""))[:300]
                _print(f"[TOOL RESULT]: {name} -> {preview}")
            content = chunk.get("content") or ""
            if chunk.get("role") not in ("tool",) and not chunk.get("tool_calls") and content:
                text = content if isinstance(content, str) else str(content)
                if text.strip():
                    final_parts.append(text)
                    _print(f"[CNK]: {text}")

        def _iter_messages(update: Any) -> Iterator[Any]:
            """Extract message-like items from a langchain Agent astream update.

            ``langchain.create_agent`` returns a LangGraph agent whose
            ``astream`` yields node-diff dicts like
            ``{"agent": {"messages": [added_messages]}}`` (plus whole-state
            envelopes in some graphs), not flat ``{"messages": [...]}``. Handle
            both the nested node form and a top-level ``messages`` key.
            """
            if not isinstance(update, dict):
                return
            top = update.get("messages")
            if isinstance(top, list):
                yield from top
            for value in update.values():
                if isinstance(value, dict):
                    inner = value.get("messages")
                    if isinstance(inner, list):
                        yield from inner

        async def _consume() -> None:
            nonlocal cancelled
            start = time.monotonic()
            def _ts() -> str:
                return f"{time.monotonic() - start:.1f}s"
            _print(f"[AGENT STREAM]: launching astream on {self._runner._thread.name} thread={threading.current_thread().name}")
            _print(f"[AGENT STREAM]: stream_input type={type(stream_input)} keys={list(stream_input) if isinstance(stream_input, dict) else 'N/A'}")
            _print(f"[AGENT STREAM]: config={config}")
            _print(f"[AGENT STREAM]: agent type={type(self._agent).__name__} graph_type={type(getattr(self._agent, 'builder', None)).__name__}")
            _updates = 0
            failure: Optional[str] = None
            try:
                _print(f"[AGENT STREAM]: calling astream() at t={_ts()}")
                # NOTE: stream_mode=["messages","updates"] was tried for live
                # token streaming but the langchain create_agent + middleware
                # graph only yields (str, metadata) tuples on the messages
                # channel (no dict updates at all), so structured events
                # (tool_call / tool_result / the final message) never arrive.
                # Plain node-diff streaming works: dict updates drive the UI
                # events and the final message. Live token deltas would need a
                # graph/middleware that also emits structured node outputs.
                astream_iter = self._agent.astream(stream_input, config=config)
                _print(f"[AGENT STREAM]: astream() returned iterator at t={_ts()}, type={type(astream_iter).__name__}")
                _print(f"[AGENT STREAM]: entering async for loop at t={_ts()}")
                async for update in astream_iter:
                    _updates += 1
                    if isinstance(update, tuple) and len(update) == 2:
                        # (AIMessageChunk, metadata) from the "messages" channel.
                        message_chunk, _meta = update
                        if isinstance(message_chunk, AIMessageChunk):
                            delta = getattr(message_chunk, "content", "")
                            if isinstance(delta, str) and delta:
                                _emit({"type": "token", "data": delta})
                        continue
                    _print(
                        f"[AGENT STREAM]: update#{_updates} t={_ts()} "
                        f"keys={list(update) if isinstance(update, dict) else type(update).__name__}"
                    )
                    before = state.revision
                    for chunk in _iter_messages(update):
                        _handle_chunk(chunk)
                    # Persist on the loop thread, right after the batch. This is
                    # the write that used to live in the script thread's
                    # `finally` - and therefore used to be lost on a rerun.
                    if state.revision != before:
                        await self._persist(state, thread_id)
                _print(f"[AGENT STREAM]: astream loop finished at t={_ts()} total_updates={_updates}")
            except asyncio.CancelledError:
                # The consumer went away (Streamlit's Stop closes the generator
                # this is feeding). Re-raise so the task ends cancelled, and set
                # the flag so the finally block does not treat an abandoned run
                # as a completed one.
                cancelled = True
                _print(f"[AGENT STREAM]: cancelled by the consumer at t={_ts()}")
                raise
            except Exception as exc:  # noqa: BLE001 - surface to UI consumer
                _print(f"[AGENT ERROR]: t={_ts()} {type(exc).__name__}: {exc}")
                import traceback
                _print(f"[AGENT ERROR] traceback:\n{traceback.format_exc()}")
                failure = f"{type(exc).__name__}: {exc}"
                _emit({"type": "error", "data": failure})
            finally:
                _print(f"[AGENT STREAM]: finally block at t={_ts()}")
                if cancelled:
                    # An abandoned run must not emit a final answer or fall back
                    # to the checkpointer. Doing either writes state for a turn
                    # the user stopped, which then shows up in the session list.
                    _print("[AGENT STREAM]: skipping final message for a cancelled run")
                elif final_parts:
                    text = "".join(final_parts)
                    _emit({"type": "message", "data": text}, [{"type": "assistant", "content": text}])
                    _print(f"[AGENT RESPONSE]: {text}")
                else:
                    # Safety net: if the stream produced no structured dict
                    # updates (e.g. a middleware that only emits raw strings on
                    # the messages channel), fall back to the last assistant
                    # message recorded by the checkpointer.
                    try:
                        state_values = await self._agent.aget_state(config)
                        values = getattr(state_values, "values", None) or {}
                        last: str | None = None
                        for m in reversed(values.get("messages", [])):
                            if isinstance(m, (AIMessage, AIMessageChunk)):
                                content = getattr(m, "content", "")
                                if isinstance(content, str) and content.strip():
                                    last = content
                                    break
                        if last is not None:
                            _emit({"type": "message", "data": last}, [{"type": "assistant", "content": last}])
                            _print(f"[AGENT RESPONSE (checkpointer)]: {last[:300]}")
                    except Exception as exc:  # noqa: BLE001 - safety net must never throw
                        _print(f"[AGENT STREAM]: checkpointer fallback failed: {exc}")
                # Final persist BEFORE marking the run terminal, so a reader that
                # sees a finished run can trust the persisted row is complete.
                try:
                    await self._persist(state, thread_id)
                except Exception as exc:  # noqa: BLE001 - never lose the run over a save failure
                    _print(f"[AGENT STREAM]: final persist failed: {exc}")
                    failure = failure or f"persist failed: {exc}"
                state.finish(error=failure)

return _consume

    def _consume(self, state: RunState, message: str, thread_id: str) -> Any:
        return self._build_consumer(state, message, thread_id)()


# ---------------------------------------------------------------------------
# Mock agent adapter with persistence
# ---------------------------------------------------------------------------

class PersistingMockAdapter:
    """Wrap ``MockAgentRunner`` to persist messages and mock tool results.

    ``MockAgentRunner.run()`` yields fake events but never writes to the
    checkpointer or the virtual filesystem.  This adapter intercepts those
    events and after each yield:

    1. Persists the accumulated message list to the checkpointer (so the
       conversation survives page reloads and shows in the session list).
    2. For mock tool calls, writes stub results to the virtual filesystem
       (so ``list_findings`` / ``list_reports`` reflect mock activity).

    The checkpoint is stored with ``source="ui"`` and a fixed checkpoint_id
    (``"ui"``) matching ``RealCheckpointerAdapter.put`` semantics.
    """

    def __init__(
        self,
        inner: Any,
        checkpointer: "RealCheckpointerAdapter",
        file_store: "RealFileStoreAdapter",
    ) -> None:
        self._inner = inner
        self._checkpointer = checkpointer
        self._file_store = file_store

    def set_tool_calls(self, tool_calls: list) -> None:
        self._inner.set_tool_calls(tool_calls)

    def set_final_message(self, message: str) -> None:
        self._inner.set_final_message(message)

    def run(self, message: str, thread_id: str) -> Iterator[Dict[str, Any]]:
        """Blocking facade over :meth:`start` (see :meth:`poll`)."""
        self.start(message, thread_id)
        index = 0
        while True:
            events, index, _rev = self.poll(thread_id, index)
            for event in events:
                yield event
            state = REGISTRY.get(thread_id)
            if state is None or not state.is_running:
                events, index, _rev = self.poll(thread_id, index)
                for event in events:
                    yield event
                return
            time.sleep(_POLL_INTERVAL)

    # -- rerun-safe API -----------------------------------------------------

    def supports_polling(self) -> bool:
        """Always True: partial mode tracks runs in the registry too."""
        return True

    def start(self, message: str, thread_id: str) -> RunState:
        """Begin a turn on a worker thread; the script thread is not involved."""
        existing: List[Dict[str, Any]] = []
        stored = self._checkpointer.get(thread_id) or {}
        existing = list(stored.get("messages", []))
        state = REGISTRY.start(
            thread_id, message, existing + [{"role": "user", "content": message}]
        )
        self._checkpointer.put(thread_id, {"messages": list(state.messages)})
        spawn(state, lambda: self._produce(message, thread_id, state),
              persist=lambda msgs: self._checkpointer.put(thread_id, {"messages": msgs}),
              name=f"mock-agent-{thread_id}")
        return state

    def poll(self, thread_id: str, index: int) -> Tuple[List[Dict[str, Any]], int, int]:
        state = REGISTRY.get(thread_id)
        if state is None:
            return [], index, 0
        events, next_index = state.events_since(index)
        return events, next_index, state.revision

    def active_run(self, thread_id: str) -> Optional[RunState]:
        return REGISTRY.get(thread_id)

    def is_running(self, thread_id: str) -> bool:
        return REGISTRY.is_running(thread_id)

    def _produce(self, message: str, thread_id: str, state: RunState) -> Iterator[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
        """Yield ``(event, rows)`` pairs, mirroring the inner mock runner."""
        for event in self._inner.run(message, thread_id):
            rows = self._rows_for(event)
            yield event, rows

    def _rows_for(self, event: Dict[str, Any]) -> List[Dict[str, Any]]:
        """Translate an event into the message rows it contributes.

        Note the shape is the historical partial-mode one (``role``/``tool_calls``)
        rather than the UI's ``type`` shape. Kept as-is so partial-mode sessions
        on disk stay readable; only the *timing* of the write changed.
        """
        event_type = event.get("type")
        rows: List[Dict[str, Any]] = []
        if event_type == "message":
            # A message event carries the text directly in "data" as a string -
            # not a nested payload like the tool events do.
            rows.append({"role": "assistant", "content": event.get("data", "")})
            return rows
        data = event.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        if event_type == "tool_call":
            name = data.get("name", "")
            args = data.get("args", {})
            rows.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{"name": name, "args": args}],
            })
            self._write_mock_tool_result(name, args)
        elif event_type == "tool_result":
            rows.append({
                "role": "tool",
                "content": str(data.get("output", "")),
                "name": data.get("name", ""),
            })
        return rows

    # -- internal helpers ---------------------------------------------------

    def _persist(
        self,
        messages: List[Dict[str, Any]],
        event: Dict[str, Any],
        thread_id: str,
    ) -> None:
        """Legacy single-event save, retained for callers outside the registry path."""
        self._rows_for(event)
        self._save_checkpoint(messages, thread_id)

    def _save_checkpoint(
        self, messages: List[Dict[str, Any]], thread_id: str
    ) -> None:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        ver = self._checkpointer._next_blob_version(thread_id)
        checkpoint = {
            "v": 1,
            "ts": ts,
            "id": "ui",
            "channel_values": {"messages": list(messages)},
            "channel_versions": {"messages": ver},
            "versions_seen": {},
            "pending_sends": [],
        }

        async def _put() -> None:
            await self._checkpointer._saver.aput(
                self._checkpointer._config(thread_id),
                checkpoint,
                {"source": "ui"},
                new_versions={"messages": ver},
            )

        self._checkpointer._runner.run(_put())

    def _write_mock_tool_result(
        self, name: str, args: Dict[str, Any]
    ) -> None:
        """Persist a stub file for mock tool results.

        ``read_file`` / ``ls`` are read-only - they must NOT overwrite the real
        agent's files already in the store, so nothing is written for them.
        Only tools that CREATE content (``write_file``, ``*_scan``) leave a
        stub in the virtual filesystem.
        """
        def _safe(content: str) -> bytes:
            return content.encode("utf-8")

        def _norm_key(path: str) -> str:
            # Mirror the agent's StoreBackend keys: /context/foo -> foo
            # (leading slash handled by RealFileStoreAdapter._norm).
            p = path.strip()
            p = p.lstrip("/")
            if p.startswith("context/"):
                p = p[len("context/") :]
            return p

        try:
            if name == "write_file":
                path = args.get("path", "")
                content_str = args.get("content", "")
                if path:
                    self._file_store.write_file(_norm_key(path), _safe(content_str))

            elif name in ("nmap_scan", "nikto_scan", "nmap", "nikto"):
                target = args.get("target") or args.get("url", "unknown")
                ext = "nmap" if "nmap" in name else "nikto"
                stub = (
                    f"# Mock {name} output for {target}\n\n"
                    f"PORT     STATE  SERVICE\n"
                    f"22/tcp   open   ssh\n"
                    f"80/tcp   open   http\n"
                    f"443/tcp  open   https\n"
                )
                self._file_store.write_file(
                    f"findings/{target.replace('.', '_')}_{ext}.txt",
                    _safe(stub),
                )
        except Exception:
            pass  # mock tool stubs must never break the stream


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
            {"name": "ls", "args": {"path": "/context/interpretations/"}},
            {"name": "read_file", "args": {"path": "/context/interpretations/recon.md"}},
            {"name": "write_file", "args": {"path": "/reports/r.md", "content": "# Recon Report (mock)\n\nGenerated by the mock agent during partial mode.\n"}},
        ])
        checkpointer = RealCheckpointerAdapter(
            runner, runtime.checkpointer, checkpoint_ns=_UI_CHECKPOINT_NS
        )
        file_store = RealFileStoreAdapter(runner, runtime.store)
        return PenTestAgentService(
            thread_id=thread_id,
            checkpointer=checkpointer,
            file_store=file_store,
            # PersistingMockAdapter makes the in-memory mock runner durable:
            # each streamed event is written to the checkpointer AND mock tool
            # results land in the virtual filesystem, so sessions survive page
            # reloads and show up in the sidebar session list.
            agent_runner=PersistingMockAdapter(agent_runner, checkpointer, file_store),
        )

    if runtime.agent is None:
        raise RuntimeError(
            "Real agent is not available. Check your .env (MISTRAL_API_KEY) "
            "and that STREAMLIT_USE_REAL_AGENT=1 / production mode is active."
        )
    # The runner persists the turn itself, from the event-loop thread, so the
    # UI-namespace row survives a Streamlit rerun that kills the script run.
    ui_checkpointer = RealCheckpointerAdapter(
        runner, runtime.checkpointer, checkpoint_ns=_UI_CHECKPOINT_NS
    )
    return PenTestAgentService(
        thread_id=thread_id,
        checkpointer=ui_checkpointer,
        file_store=RealFileStoreAdapter(runner, runtime.store),
        agent_runner=RealAgentRunnerAdapter(runner, runtime.agent, checkpointer=ui_checkpointer),
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
        # Report the backend too. The backend decides which model ids are even
        # valid, so "configured (model: X)" without it is not enough to tell a
        # working setup from a stale AGENT_MODEL carried over from another
        # backend - that combination fails at request time with a 404, not here.
        backend = getattr(runtime, "llm_backend", None) or "?"
        lines.append(f"llm api: configured (backend: {backend}, model: {model})")
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