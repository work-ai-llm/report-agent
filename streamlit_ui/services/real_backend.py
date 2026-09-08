"""Real-backend adapters: wire the actual agent persistence into the Streamlit UI.

This is the "partial swap": the UI keeps the mock agent runner (no LLM / no API
keys needed) but talks to the REAL persistence layer that the production agent
uses - LangGraph's async SQLite (or Postgres) checkpointer for session history
and the async store for the virtual filesystem.

## Why a dedicated event loop thread

``agent.report_agent.AgentRuntime.initialize_persistence()`` opens aiosqlite
connections (and/or an async Postgres pool). Those bind to the event loop they
were created on, but Streamlit runs synchronous scripts. So every async call
runs on one persistent background loop, driven from the sync UI thread via
``asyncio.run_coroutine_threadsafe``.

## Wiring

```
app.py
  └─ _shared_real_backend() ── st.cache_resource ──▶ get_real_backend()
                                                        │ (loop thread + AgentRuntime)
                                                        ▼
                                              RealCheckpointerAdapter  ──▶ saver.get/put
                                              RealFileStoreAdapter     ──▶ store.get/put
```

## File store format

Files live in namespace ``("filesystem",)`` as v2 values, the exact format the
real agent's ``FilesystemMiddleware`` (deepagents ``StoreBackend``) writes:
``{"content": <str>, "encoding": "utf-8"}``. Keys are paths like
``findings/nmap.txt`` / ``reports/report.md``, so files the agent later writes
appear in the UI and vice versa.
"""

import asyncio
import logging
import os
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from streamlit_ui.services.agent_service import PenTestAgentService
from streamlit_ui.services.interfaces import (
    CheckpointerProtocol,
    FileStoreProtocol,
)

logger = logging.getLogger(__name__)

_NAMESPACE = ("filesystem",)

_TRUE_BACKEND_MODES = ("1", "true", "yes")


def is_real_backend_mode() -> bool:
    """Return True when the app should run the partial (real persistence) swap.

    Enabled by ``STREAMLIT_USE_REAL_BACKEND=1`` (also ``true``/``yes``). Takes
    precedence together with mock mode: mock mode (``STREAMLIT_USE_MOCKS=1``)
    is checked first in the app, so the two are mutually exclusive.
    """
    return os.environ.get("STREAMLIT_USE_REAL_BACKEND", "").strip().lower() in _TRUE_BACKEND_MODES


def backend_label(runtime: Any) -> str:
    """Human-readable backend description for the debug status report.

    Args:
        runtime: An initialized ``AgentRuntime`` instance.

    Returns:
        ``"postgres: <masked uri>"`` when a pool is live, else
        ``"sqlite: <path>"``.
    """
    if runtime.pool is not None:
        from agent import report_agent as ra

        uri = (ra.DB_URI or "unknown").split("?", 1)[0]
        return f"postgres: {uri}"
    from agent import report_agent as ra

    return f"sqlite: {ra.LOCAL_DB_PATH}"


class EventLoopThread:
    """Persistent asyncio loop running on a daemon thread.

    All real LangGraph async operations (checkpointer + store) are scheduled on
    this loop so the aiosqlite connections / Postgres pool stay attached to a
    single loop, while the sync Streamlit thread drives them via ``run()``.
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
        """Run an awaitable on the persistent loop from any thread.

        Args:
            coro: A coroutine to execute on the background loop.
            timeout: Maximum seconds to block waiting for the result.

        Returns:
            The coroutine's return value. Raises the coroutine's exception.

        Raises:
            TimeoutError: if the operation exceed ``timeout`` seconds.
        """
        if self._loop is None:  # pragma: no cover - defensive
            raise RuntimeError("Event loop not ready")
        future = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return future.result(timeout)


class RealCheckpointerAdapter:
    """Adapt a real LangGraph async checkpointer to ``CheckpointerProtocol``.

    Session state is persisted as a real LangGraph checkpoint whose
    ``channel_values["messages"]`` holds the UI conversation. ``list_threads``
    scans all checkpoint pages and de-dupes thread ids.
    """

    def __init__(self, runner: EventLoopThread, saver: Any) -> None:
        self._runner = runner
        self._saver = saver

    @staticmethod
    def _config(thread_id: str) -> Dict[str, Any]:
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}

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
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        checkpoint = {
            "v": 1,
            "ts": ts,
            "id": ts + "-" + uuid.uuid4().hex[:6],
            "channel_values": {"messages": messages},
            "channel_versions": {"messages": 1},
            "versions_seen": {},
            "pending_sends": [],
        }

        async def _put() -> None:
            # The UI keeps exactly one state per thread: drop any previous
            # checkpoint before writing, so aget_tuple never has to pick a
            # "latest" among several versions.
            await self._saver.adelete_thread(thread_id)
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
            await self._saver.adelete_thread(thread_id)
            return True

        return self._runner.run(_delete())


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
# Lifecycle helpers
# ---------------------------------------------------------------------------

_CACHE_LOCK = threading.Lock()
_CACHE: Optional[Tuple[EventLoopThread, Any]] = None


def create_real_backend(db_path: Optional[str] = None, timeout: float = 60.0) -> Tuple[EventLoopThread, Any]:
    """Create a fresh runtime + event loop, initializing persistence only.

    This does NOT build the LLM or the agent - only observability + the
    checkpointer/store (see ``AgentRuntime.initialize_persistence``). No model
    API keys are required.

    Args:
        db_path: Override the SQLite file used by the runtime. Falls back to
                 ``REPORT_AGENT_DB_PATH`` env var, then the agent default
                 (``agent/agent_session.db``). Postgres is still used when
                 ``POSTGRES_URI`` is set and reachable.
        timeout: Max seconds to block on ``initialize_persistence()``.

    Returns:
        ``(runner, runtime)`` - the loop thread and the initialized
        ``AgentRuntime``.
    """
    path = db_path or os.environ.get("REPORT_AGENT_DB_PATH")
    if path:
        from agent import report_agent as ra

        ra.LOCAL_DB_PATH = Path(path)

    from agent.report_agent import AgentRuntime

    runtime = AgentRuntime()
    runner = EventLoopThread()
    runner.run(runtime.initialize_persistence(), timeout=timeout)
    logger.debug("real backend initialized: %s", backend_label(runtime))
    return runner, runtime


def get_real_backend() -> Tuple[EventLoopThread, Any]:
    """Return the process-wide real backend, creating it on first call.

    Cached so the app, the debug probe, and every session share one event loop
    and one set of DB connections.
    """
    global _CACHE
    if _CACHE is None:
        with _CACHE_LOCK:
            if _CACHE is None:
                _CACHE = create_real_backend()
    return _CACHE


def build_real_backend_service(thread_id: str) -> PenTestAgentService:
    """Build a service with REAL checkpointer + store, but the MOCK agent.

    This is the partial swap: sessions and the virtual filesystem persist to the
    real DB (survive restarts), while the agent replies are still scripted by
    ``MockAgentRunner``. The final swap replaces the mock runner with a real
    ``AgentRunnerProtocol`` adapter around ``runtime.agent``.
    """
    from tests.mock.mock_objects_streamlit import MockAgentRunner

    runner, runtime = get_real_backend()
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