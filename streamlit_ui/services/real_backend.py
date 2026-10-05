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
        async def _get() -> Optional[Any]:
            return await self._saver.aget_tuple(self._config(thread_id))

        tup = self._runner.run(_get())
        if tup is not None and tup.checkpoint is not None:
            base = (tup.checkpoint.get("channel_versions") or {}).get("messages", 0)
            try:
                return int(base) + 1
            except (TypeError, ValueError):
                return 1
        return 1

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
        ver = self._next_blob_version(thread_id)
        checkpoint = {
            "v": 1,
            "ts": ts,
            "id": "ui",
            "channel_values": {"messages": messages},
            "channel_versions": {"messages": ver},
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
                new_versions={"messages": ver},
            )

        self._runner.run(_put())

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
        from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

        emit_print = is_agent_output_print_enabled()
        trace_path = os.environ.get("STREAMLIT_AGENT_LOG")
        events: "queue.Queue[Any]" = queue.Queue()
        sentinel = object()
        final_parts: List[str] = []
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

        def _handle_chunk(chunk: Any) -> None:
            if isinstance(chunk, dict):
                _handle_dict_chunk(chunk)
                return
            if isinstance(chunk, ToolMessage):
                name = getattr(chunk, "name", "") or ""
                output = getattr(chunk, "content", "")
                events.put({"type": "tool_result", "data": {"name": name, "output": output}})
                preview = str(output)[:300]
                _print(f"[TOOL RESULT]: {name} -> {preview}")
                return

            if isinstance(chunk, (AIMessage, AIMessageChunk)):
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

        def _handle_dict_chunk(chunk: Dict[str, Any]) -> None:
            # Serialized message form (some graph updates yield dicts).
            if chunk.get("tool_calls"):
                for tc in chunk["tool_calls"]:
                    data = {"name": tc.get("name") or tc.get("function", {}).get("name"),
                            "args": tc.get("args") or tc.get("function", {}).get("arguments", {})}
                    events.put({"type": "tool_call", "data": data})
                    _print(f"[TOOL CALL]: {data['name']} args={data['args']}")
            if chunk.get("role") == "tool":
                name = chunk.get("name") or ""
                events.put({"type": "tool_result", "data": {"name": name, "output": chunk.get("content", "")}})
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
            start = time.monotonic()
            def _ts() -> str:
                return f"{time.monotonic() - start:.1f}s"
            _print(f"[AGENT STREAM]: launching astream on {self._runner._thread.name} thread={threading.current_thread().name}")
            _print(f"[AGENT STREAM]: stream_input type={type(stream_input)} keys={list(stream_input) if isinstance(stream_input, dict) else 'N/A'}")
            _print(f"[AGENT STREAM]: config={config}")
            _print(f"[AGENT STREAM]: agent type={type(self._agent).__name__} graph_type={type(getattr(self._agent, 'builder', None)).__name__}")
            _updates = 0
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
                                events.put({"type": "token", "data": delta})
                        continue
                    _print(
                        f"[AGENT STREAM]: update#{_updates} t={_ts()} "
                        f"keys={list(update) if isinstance(update, dict) else type(update).__name__}"
                    )
                    for chunk in _iter_messages(update):
                        _handle_chunk(chunk)
                _print(f"[AGENT STREAM]: astream loop finished at t={_ts()} total_updates={_updates}")
            except Exception as exc:  # noqa: BLE001 - surface to UI consumer
                _print(f"[AGENT ERROR]: t={_ts()} {type(exc).__name__}: {exc}")
                import traceback
                _print(f"[AGENT ERROR] traceback:\n{traceback.format_exc()}")
                events.put({"type": "error", "data": f"{type(exc).__name__}: {exc}"})
            finally:
                _print(f"[AGENT STREAM]: finally block at t={_ts()}")
                if final_parts:
                    text = "".join(final_parts)
                    events.put({"type": "message", "data": text})
                    _print(f"[AGENT RESPONSE]: {text}")
                else:
                    # Safety net: if the stream produced no structured dict
                    # updates (e.g. a middleware that only emits raw strings on
                    # the messages channel), fall back to the last assistant
                    # message recorded by the checkpointer.
                    try:
                        state = await self._agent.aget_state(config)
                        values = getattr(state, "values", None) or {}
                        last: str | None = None
                        for m in reversed(values.get("messages", [])):
                            if isinstance(m, (AIMessage, AIMessageChunk)):
                                content = getattr(m, "content", "")
                                if isinstance(content, str) and content.strip():
                                    last = content
                                    break
                        if last is not None:
                            events.put({"type": "message", "data": last})
                            _print(f"[AGENT RESPONSE (checkpointer)]: {last[:300]}")
                    except Exception as exc:  # noqa: BLE001 - safety net must never throw
                        _print(f"[AGENT STREAM]: checkpointer fallback failed: {exc}")
                events.put(sentinel)

        self._runner.schedule(_consume())

        while True:
            item = events.get()
            if item is sentinel:
                break
            yield item


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
        # Seed with the EXISTING persisted history (so mid-stream checkpoints
        # keep the whole conversation, not just the current turn), then append
        # the new user message.
        existing = (self._checkpointer.get(thread_id) or {}).get("messages", [])
        messages: List[Dict[str, Any]] = list(existing)
        messages.append({"role": "user", "content": message})

        for event in self._inner.run(message, thread_id):
            yield event
            self._persist(messages, event, thread_id)

    # -- internal helpers ---------------------------------------------------

    def _persist(
        self,
        messages: List[Dict[str, Any]],
        event: Dict[str, Any],
        thread_id: str,
    ) -> None:
        event_type = event.get("type")

        if event_type == "tool_call":
            name = event.get("data", {}).get("name", "")
            args = event.get("data", {}).get("args", {})
            messages.append({
                "role": "assistant",
                "content": None,
                "tool_calls": [{"name": name, "args": args}],
            })
            self._write_mock_tool_result(name, args)

        elif event_type == "tool_result":
            name = event.get("data", {}).get("name", "")
            output = event.get("data", {}).get("output", "")
            messages.append({"role": "tool", "content": str(output), "name": name})

        elif event_type == "message":
            messages.append({"role": "assistant", "content": event.get("data", "")})

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