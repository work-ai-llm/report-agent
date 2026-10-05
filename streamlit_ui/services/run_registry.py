"""Rerun-surviving run registry for agent turns.

## Why this exists

A Streamlit script run is linear and synchronous. Any widget interaction queues a
rerun, and the queued rerun *abandons* the current script mid-execution: the
browser drops the elements already pushed for that run, and the Python stack
unwinds. Anything the abandoned run had not yet persisted is gone from the UI.

The agent itself was never affected. ``RealAgentRunnerAdapter`` schedules the
LangGraph stream onto the persistent ``EventLoopThread`` and returns immediately,
so the run keeps going and keeps checkpointing into the agent's own namespace
after the UI has forgotten about it. The result was a permanently diverged pair
of histories: the browser showed less than the checkpointer held, and the UI
namespace was never reconciled against the agent namespace.

So the fix cannot be "don't let the script die" - it must be "don't let the
script be the thing that owns the run". This module holds the run state somewhere
that outlives a script run.

## The model

- ``RunState`` - one in-flight (or just-finished) turn. Owns the events produced
  so far and the UI-namespace message list being built for that turn. Written by
  the event-loop thread, read by the Streamlit script thread, hence a lock.
- ``RunRegistry`` - ``thread_id -> RunState``, process-wide. A Streamlit session
  and every rerun of it see the same object, because the registry is a module
  singleton rather than ``st.session_state``.

``revision`` is the cheap "has anything changed" signal the UI polls. It is
monotonic per thread and bumped on every recorded event and on completion, so a
rerun can decide whether the persisted UI history it is holding is stale without
touching the database.

## What this module deliberately does not do

It does not decide *when* to persist. Persistence is owned by the adapter, which
is the only thing that knows the checkpointer. The registry just holds the
message list that gets persisted, so that the adapter and the UI agree on what
"the conversation so far" means.

A background thread must never touch ``st.session_state`` - ``SafeSessionState``
is script-thread scoped. Nothing here imports Streamlit, which is what keeps that
rule enforceable.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

# Run lifecycle values. ``RUNNING`` is the only non-terminal state.
RUNNING = "running"
DONE = "done"
ERROR = "error"


class RunState:
    """One agent turn: its events and the UI message list built from them.

    Thread-safe. The event-loop thread appends via :meth:`record_event` and
    :meth:`finish`; the Streamlit script thread reads via :meth:`events_since`
    and :meth:`snapshot`.
    """

    def __init__(self, thread_id: str, message: str, messages: Optional[List[Dict[str, Any]]] = None) -> None:
        self.run_id: str = uuid.uuid4().hex
        self.thread_id: str = thread_id
        self.message: str = message
        self.started_at: float = time.monotonic()
        self.finished_at: Optional[float] = None

        self._lock = threading.Lock()
        # The UI-namespace conversation, seeded with existing history plus the
        # new user turn. Appended to as events arrive; this exact list is what
        # gets persisted, so the adapter and the UI never disagree about it.
        self.messages: List[Dict[str, Any]] = list(messages or [])
        self.events: List[Dict[str, Any]] = []
        self.status: str = RUNNING
        self.error: Optional[str] = None
        self._revision: int = 0

    # -- writes (event-loop thread) ----------------------------------------

    def record_event(
        self,
        event: Dict[str, Any],
        additions: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Record one streamed event plus the message rows it contributes.

        Args:
            event: The UI event dict (``tool_call`` / ``tool_result`` / ``token``
                / ``message`` / ``error``).
            additions: Rows to append to :attr:`messages`. ``token`` deltas
                contribute nothing - they are a display-time concern and would
                otherwise multiply the persisted row count.
        """
        with self._lock:
            self.events.append(event)
            if additions:
                self.messages.extend(additions)
            self._revision += 1

    def finish(self, error: Optional[str] = None) -> None:
        """Mark the run terminal. Idempotent - the first call wins.

        Called by the producer after its final persist, so a reader that sees a
        terminal status can trust the persisted list is complete.
        """
        with self._lock:
            if self.status != RUNNING:
                return
            self.status = ERROR if error else DONE
            self.error = error
            self.finished_at = time.monotonic()
            self._revision += 1

    # -- reads (script thread) ---------------------------------------------

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self.status == RUNNING

    def events_since(self, index: int) -> Tuple[List[Dict[str, Any]], int]:
        """Return ``(events[index:], len(events))`` for incremental consumption."""
        with self._lock:
            return list(self.events[index:]), len(self.events)

    def snapshot(self) -> Dict[str, Any]:
        """A consistent shallow copy, for tests and diagnostics."""
        with self._lock:
            return {
                "run_id": self.run_id,
                "thread_id": self.thread_id,
                "status": self.status,
                "error": self.error,
                "revision": self._revision,
                "events": list(self.events),
                "messages": list(self.messages),
            }


class RunRegistry:
    """Process-wide ``thread_id -> RunState`` map.

    Only the latest run per thread is retained. That is enough for the UI's needs
    (render progress, notice completion) and keeps a long-lived server from
    accumulating one entry per turn forever. A finished run is intentionally kept
    rather than dropped: the UI needs to see the terminal state at least once so
    a rerun can pick up the final answer.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: Dict[str, RunState] = {}

    def start(
        self,
        thread_id: str,
        message: str,
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> RunState:
        """Register a new run for ``thread_id``, replacing any previous one."""
        state = RunState(thread_id, message, messages)
        with self._lock:
            self._runs[thread_id] = state
        return state

    def get(self, thread_id: str) -> Optional[RunState]:
        with self._lock:
            return self._runs.get(thread_id)

    def revision(self, thread_id: str) -> int:
        """Monotonic change counter for a thread; ``0`` when it never ran."""
        state = self.get(thread_id)
        return state.revision if state is not None else 0

    def is_running(self, thread_id: str) -> bool:
        state = self.get(thread_id)
        return state.is_running if state is not None else False

    def running_threads(self) -> List[str]:
        with self._lock:
            states = list(self._runs.values())
        return sorted(s.thread_id for s in states if s.is_running)

    def drop(self, thread_id: str) -> None:
        """Forget a thread's run (called when the session is deleted)."""
        with self._lock:
            self._runs.pop(thread_id, None)

    def clear(self) -> None:
        with self._lock:
            self._runs.clear()


# The one registry the whole process shares. Deliberately a module singleton and
# NOT st.session_state / st.cache_resource: a run must outlive both a rerun and
# the session object that started it.
REGISTRY = RunRegistry()


# ---------------------------------------------------------------------------
# Driver for synchronous producers
# ---------------------------------------------------------------------------

def drive(
    state: RunState,
    produce: Any,
    persist: Any = None,
    poll_interval: float = 0.05,
) -> None:
    """Drain a synchronous event producer into ``state``, on a worker thread.

    The synchronous mock runners are generators that need a real thread (they
    ``time.sleep`` to imitate latency). Once the producing happens off the script
    thread, the script thread is free to die on a rerun without losing anything:
    this body keeps recording and persisting regardless.

    Args:
        state: The run to record into.
        produce: Callable returning an iterator of ``(event, additions)`` pairs,
            where ``additions`` is the list of message rows the event contributes.
        persist: Optional callable taking the current message list, called after
            each batch. Must be safe to call from this worker thread.
        poll_interval: Unused; kept so callers can document their cadence. The
            generator drives its own pacing.
    """
    del poll_interval
    failure: Optional[str] = None
    try:
        for event, additions in produce():
            state.record_event(event, additions)
            if persist is not None:
                persist(list(state.messages))
    except Exception as exc:  # noqa: BLE001 - a dead producer must still end the run
        failure = f"{type(exc).__name__}: {exc}"
        state.record_event({"type": "error", "data": failure})
    finally:
        if persist is not None:
            try:
                persist(list(state.messages))
            except Exception:  # noqa: BLE001 - never lose the run over a save failure
                pass
        state.finish(error=failure)


def spawn(state: RunState, produce: Any, persist: Any = None, name: str = "agent-run") -> threading.Thread:
    """Run :func:`drive` on a daemon thread and return it.

    Daemon so a forgotten in-flight turn can never stop the process exiting.
    """
    thread = threading.Thread(target=drive, args=(state, produce, persist), name=name, daemon=True)
    thread.start()
    return thread