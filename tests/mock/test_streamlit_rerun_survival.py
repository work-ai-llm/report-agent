"""End-to-end Streamlit tests for rerun survival (issue #20).

These drive the real ``streamlit_ui/app.py`` through ``AppTest`` in mock mode and
interrupt an in-flight turn the way a user does: by clicking a sidebar widget
while the agent is still streaming.

## Why an AppTest can reproduce this

A queued rerun does not kill the script thread outright - Streamlit records the
request and services it at the next element enqueue, raising ``RerunException``.
Clicking a sidebar widget from another ``AppTest`` interaction therefore raises
the same exception inside the streaming loop that a real click would.

## What these tests can and cannot prove

An honest note, because it shapes the whole design of this file.

``AppTest.run()`` is **synchronous**: it blocks until the script run finishes. A
mid-stream interruption therefore cannot be produced from a test by clicking a
widget - there is no second thread to click it from, and ``.click()`` without
``.run()`` only queues the interaction without executing anything. So the
*interruption itself* is simulated one layer down, where it can be done honestly:

- :mod:`tests.mock.test_agent_run_syncing` abandons the consumer entirely and
  lets the run finish unattended. That is a strict superset of "the script died":
  the agent keeps going and nobody is reading.
- ``tests/acceptance/9_streamlit_agent_syncing.py`` does the same against the real
  LLM and a real checkpointer.

What the tests below *do* verify is everything on the UI side of the fix: that the
persisted transcript is complete and correctly shaped, that a rerun reconciles the
session against it, and that threads stay isolated. Those are real assertions
about real behaviour - they are just not the interruption itself.

Note these tests depend on the mock runner's per-event delay to create a window.
The delay is raised here (rather than the app's 0.35s demo default) purely so the
turn is still running when the queued widget interaction is serviced; the
assertions do not depend on exactly which event was in flight.
"""

from __future__ import annotations

import copy
import os
import time
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

os.environ["STREAMLIT_USE_MOCKS"] = "1"

APP_PATH = str(Path(__file__).resolve().parent.parent.parent / "streamlit_ui" / "app.py")


@pytest.fixture(autouse=True)
def _isolate():
    try:
        st.cache_resource.clear()
    except Exception:
        pass
    os.environ["STREAMLIT_USE_MOCKS"] = "1"
    from streamlit_ui.services.run_registry import REGISTRY

    REGISTRY.clear()
    yield
    try:
        st.cache_resource.clear()
    except Exception:
        pass
    REGISTRY.clear()


@pytest.fixture
def writes(monkeypatch):
    """Record every UI-namespace write the app makes.

    ``_shared_mock_checkpointer`` is a ``st.cache_resource``, and Streamlit does
    not deduplicate those when they are called from outside a script run - so the
    instance the app used is not reachable from the test. Spying on the class
    sidesteps that: it sees the writes regardless of which instance received
    them, which is exactly the assertion we want (the app persisted the turn).
    """
    from tests.mock.mock_objects_streamlit import MockCheckpointer

    log: list = []
    original = MockCheckpointer.put

    def _spy(self, thread_id, state):
        log.append((thread_id, copy.deepcopy(list(state.get("messages", [])))))
        return original(self, thread_id, state)

    monkeypatch.setattr(MockCheckpointer, "put", _spy)
    return log


def _wait_until(predicate, timeout: float = 20.0, interval: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def _persisted_rows(thread_id: str, writes) -> list:
    """The most recent UI-namespace write for ``thread_id``."""
    for tid, messages in reversed(writes):
        if tid == thread_id:
            return messages
    return []


def _slow_runner(delay: float, monkeypatch):
    """Widen (or remove) the mock runner's per-event delay.

    Set via env var because ``_shared_mock_agent_runner`` is a
    ``st.cache_resource``: Streamlit does not dedupe those when they are called
    from outside a script run, so the instance the app used is not reachable
    from a test and cannot be reconfigured directly.
    """
    monkeypatch.setenv("STREAMLIT_MOCK_DELAY", str(delay))


class TestInterruptedTurnIsNotLost:
    def test_widget_click_mid_stream_does_not_lose_the_turn(self, writes, monkeypatch):
        """The #20 regression, at the UI level.

        Send a prompt, interrupt the streaming script run by clicking a sidebar
        widget, then confirm the full turn reached the checkpointer - user prompt
        included - even though the browser-side run was abandoned.
        """
        # Set BEFORE the first run: the cached mock runner reads the env var once,
        # when Streamlit first builds it.
        _slow_runner(0.4, monkeypatch)
        at = AppTest.from_file(APP_PATH).run()

        at.chat_input[0].set_value("list the findings").run()
        thread_id = at.session_state["current_thread_id"]
        assert thread_id is not None

        from streamlit_ui.services.run_registry import REGISTRY

        # Interrupt: a sidebar widget click queues a rerun, which unwinds the
        # streaming script run.
        at.sidebar.button[0].click()

        # Let the abandoned run finish on its own.
        assert _wait_until(lambda: not REGISTRY.is_running(thread_id)), "run never completed"

        rows = _persisted_rows(thread_id, writes)
        types = [r.get("type") for r in rows]
        assert types, "nothing was persisted for the interrupted turn"
        assert types[0] == "user"
        assert rows[0]["content"] == "list the findings"
        assert types[-1] == "assistant"

    def test_rerun_after_interruption_renders_the_completed_turn(self, writes, monkeypatch):
        """The UI converges on the persisted transcript when the user returns.

        ``New Session`` deliberately empties the view, so "coming back" means
        re-selecting the thread. That re-read is the reconciliation path: the
        turn that streamed while the script was dead is there in full.
        """
        _slow_runner(0.4, monkeypatch)
        at = AppTest.from_file(APP_PATH).run()

        at.chat_input[0].set_value("first question").run()
        thread_id = at.session_state["current_thread_id"]
        at.sidebar.button[0].click()  # New Session, mid-turn

        from streamlit_ui.services.run_registry import REGISTRY

        assert _wait_until(lambda: not REGISTRY.is_running(thread_id))

        at.run()  # let the selector list both threads
        at.sidebar.selectbox[0].select(thread_id).run()

        types = [m.get("type") for m in at.session_state["chat_messages"]]
        assert types[0] == "user"
        assert types[-1] == "assistant"
        assert "tool_call" in types

    def test_new_session_click_does_not_destroy_the_running_turn(self, writes, monkeypatch):
        """The destructive-callback case from #20.

        ``New Session`` used to reassign ``chat_messages`` while the interrupted
        run's ``finally`` was still unwinding, which is how a whole turn
        (including the human prompt) was dropped from the stored row.
        """
        _slow_runner(0.4, monkeypatch)
        at = AppTest.from_file(APP_PATH).run()

        at.chat_input[0].set_value("turn one").run()
        first_thread = at.session_state["current_thread_id"]

        # New Session while the first turn is still going.
        at.sidebar.button[0].click()

        from streamlit_ui.services.run_registry import REGISTRY

        assert _wait_until(lambda: not REGISTRY.is_running(first_thread))

        rows = _persisted_rows(first_thread, writes)
        assert rows, "the interrupted turn's row was wiped"
        assert rows[0]["content"] == "turn one"
        assert rows[-1]["type"] == "assistant"

    def test_second_message_appends_rather_than_replaces(self, writes, monkeypatch):
        """History accumulates across turns in one thread."""
        _slow_runner(0.0, monkeypatch)
        at = AppTest.from_file(APP_PATH).run()

        at.chat_input[0].set_value("one").run()
        at.chat_input[0].set_value("two").run()
        thread_id = at.session_state["current_thread_id"]

        rows = _persisted_rows(thread_id, writes)
        users = [r["content"] for r in rows if r.get("type") == "user"]
        assert users == ["one", "two"]
        # The shared mock runner emits two tool calls per turn, so two turns give
        # four rows - and exactly one assistant reply per turn.
        assert sum(1 for r in rows if r.get("type") == "tool_call") == 4
        assert sum(1 for r in rows if r.get("type") == "assistant") == 2

    def test_tool_results_are_attached_to_their_tool_call(self, writes, monkeypatch):
        """The transcript keeps args + result together (store_cli reads these)."""
        at = AppTest.from_file(APP_PATH).run()
        _slow_runner(0.0, monkeypatch)
        at.chat_input[0].set_value("scan").run()
        thread_id = at.session_state["current_thread_id"]

        rows = _persisted_rows(thread_id, writes)
        tool_calls = [r for r in rows if r.get("type") == "tool_call"]
        assert tool_calls
        assert all("name" in tc and "args" in tc for tc in tool_calls)


class TestSessionIsolationUnderRuns:
    def test_two_threads_keep_independent_transcripts(self, writes, monkeypatch):
        """Run a turn in A, switch to B, run in B: neither row is contaminated."""
        _slow_runner(0.0, monkeypatch)
        at = AppTest.from_file(APP_PATH).run()

        at.chat_input[0].set_value("question A").run()
        thread_a = at.session_state["current_thread_id"]

        at.sidebar.button[0].click()  # New Session
        at.run()
        at.chat_input[0].set_value("question B").run()
        thread_b = at.session_state["current_thread_id"]
        assert thread_a != thread_b

        rows_a = _persisted_rows(thread_a, writes)
        rows_b = _persisted_rows(thread_b, writes)
        assert [r["content"] for r in rows_a if r.get("type") == "user"] == ["question A"]
        assert [r["content"] for r in rows_b if r.get("type") == "user"] == ["question B"]

    def test_switching_back_restores_the_full_turn(self, writes, monkeypatch):
        _slow_runner(0.0, monkeypatch)
        at = AppTest.from_file(APP_PATH).run()

        at.chat_input[0].set_value("alpha").run()
        thread_a = at.session_state["current_thread_id"]
        at.sidebar.button[0].click()
        at.run()
        at.sidebar.selectbox[0].select(thread_a).run()

        types = [m.get("type") for m in at.session_state["chat_messages"]]
        assert types == ["user", "tool_call", "tool_call", "assistant"]