"""Tests for the live run-status strip and the sidebar-rerun path (issue #20).

Two behaviours that only exist once the run registry is in place:

1. **The app keeps saying the agent is working.** Streamlit has no push channel,
   so a script run that returns leaves the UI looking idle even though the turn is
   still running on the event-loop thread. That is the "it just disappears"
   complaint from #20. ``render_run_status`` is a ``st.fragment`` that re-reads
   the registry on a timer, so the strip survives the script run ending.

2. **A sidebar-triggered rerun cannot skip reconciliation.** A successful upload
   ends in ``st.rerun()`` from inside the sidebar, which unwinds the script before
   anything placed after ``render_sidebar`` runs. Reconciliation therefore has to
   happen *before* the sidebar, or an upload can leave the chat area blank.

Note on coverage: ``AppTest.run()`` is synchronous and blocks until the script
finishes, so these tests set up run state directly in the registry rather than
trying to catch a turn genuinely mid-flight. What is asserted is the UI's reaction
to that state, which is the part that was broken.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from tests.mock.mock_objects_streamlit import MockAgentRunner, MockCheckpointer

os.environ["STREAMLIT_USE_MOCKS"] = "1"

APP_PATH = str(Path(__file__).resolve().parent.parent.parent / "streamlit_ui" / "app.py")


@pytest.fixture(autouse=True)
def _isolate():
    try:
        st.cache_resource.clear()
    except Exception:
        pass
    os.environ["STREAMLIT_USE_MOCKS"] = "1"
    os.environ["STREAMLIT_MOCK_DELAY"] = "0"
    from streamlit_ui.services.run_registry import REGISTRY

    REGISTRY.clear()
    yield
    try:
        st.cache_resource.clear()
    except Exception:
        pass
    REGISTRY.clear()


def _status_texts(at) -> str:
    """All text the app rendered inside status widgets.

    ``AppTest``'s Status element exposes ``label`` (not ``value``), and the step
    count lives in a caption child rather than the label.
    """
    parts = []
    for el in at.status:
        parts.append(str(el.label))
        for child in el.children.values():
            parts.append(str(getattr(child, "value", getattr(child, "label", ""))))
    return " ".join(parts)


def _active_run(thread_id: str):
    """Register an in-flight run for ``thread_id`` and return it."""
    from streamlit_ui.services.run_registry import REGISTRY, RunState

    state = RunState(thread_id, "a question", [{"type": "user", "content": "a question"}])
    state.record_event({"type": "tool_call", "data": {"name": "ls", "args": {}}})
    state.record_event({"type": "tool_call", "data": {"name": "read_file", "args": {}}})
    REGISTRY.start(thread_id, "a question", state.messages)
    live = REGISTRY.get(thread_id)
    live.record_event({"type": "tool_call", "data": {"name": "ls", "args": {}}})
    live.record_event({"type": "tool_call", "data": {"name": "read_file", "args": {}}})
    return live


class _CountingCheckpointer(MockCheckpointer):
    """MockCheckpointer that tallies reads, so idle reruns can be shown to skip them."""

    def __init__(self):
        super().__init__()
        self.reads = 0

    def get(self, thread_id):
        self.reads += 1
        return super().get(thread_id)


def _service(thread_id: str = "t"):
    from streamlit_ui.services.agent_service import PenTestAgentService
    from tests.mock.mock_objects_streamlit import MockAgentRunner, MockFileStore

    return PenTestAgentService(
        thread_id=thread_id,
        checkpointer=_CountingCheckpointer(),
        file_store=MockFileStore(),
        agent_runner=MockAgentRunner(),
    )


# Harness state lives on an importable module, not here: AppTest writes the
# harness to a temp file and execs it standalone, so this module's globals are
# invisible to it.
def _reconcile_harness() -> None:
    """Standalone script that drives ``_reconcile_with_persisted`` once."""
    import streamlit as st

    import streamlit_ui.app as app_module
    from tests.mock.mock_objects_streamlit import HARNESS

    st.session_state.current_thread_id = HARNESS["thread_id"]
    st.session_state._seen_run_revision = HARNESS["seen"]
    st.session_state.chat_messages = []

    app_module._reconcile_with_persisted(HARNESS["svc"])

    # Read the counter BEFORE the harness does its own observational read, so
    # "reconciled without a reason" cannot be masked by the instrumentation.
    st.session_state["reads_after_reconcile"] = HARNESS["svc"]._checkpointer.reads
    st.session_state["observed"] = HARNESS["svc"]._checkpointer.get(HARNESS["thread_id"])
    st.session_state["seen_after"] = st.session_state["_seen_run_revision"]


def _run_harness(holder: dict):
    from tests.mock.mock_objects_streamlit import HARNESS

    HARNESS.clear()
    HARNESS.update(holder)
    return AppTest.from_function(_reconcile_harness).run()


class TestRunStatusStrip:
    def test_strip_reports_a_running_turn(self):
        """The whole point: an in-flight turn stays visible after the run ends."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("a question").run()
        thread_id = at.session_state["current_thread_id"]

        # The turn finished during that script run. Now simulate one still in
        # flight (what the user sees after an interrupted turn) and re-render.
        _active_run(thread_id)
        at.run()

        text = _status_texts(at)
        assert "Agent is running" in text, f"no running indicator rendered: {text!r}"
        # It should also say something useful about progress.
        assert "2 step(s)" in text

    def test_strip_is_absent_when_idle(self):
        """An idle app must not look busy - and must not cost a visible element."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("a question").run()
        at.run()
        assert "Agent is running" not in _status_texts(at)

    def test_no_strip_without_an_active_thread(self):
        at = AppTest.from_file(APP_PATH).run()
        assert at.session_state["current_thread_id"] is None
        assert "Agent is running" not in _status_texts(at)

    def test_finishing_transition_is_one_shot(self):
        """Running → done triggers exactly one rerun, not a loop.

        The transition is driven by a flag that is popped on use; if it were not,
        every fragment tick would rerun the whole app forever.
        """
        from streamlit_ui.services.run_registry import REGISTRY

        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("a question").run()
        thread_id = at.session_state["current_thread_id"]

        _active_run(thread_id)
        at.run()
        assert "Agent is running" in _status_texts(at)

        # The run finishes. The fragment sees the transition and asks for one full
        # rerun, which reconciles and then settles.
        REGISTRY.get(thread_id).finish()
        at.run()
        at.run()

        assert "Agent is running" not in _status_texts(at)
        at.run()
        assert at.session_state["_run_was_active"] is False


class TestSecondMessageIsRefusedWhileRunning:
    """Submitting a second prompt mid-turn must not start a second run.

    Regression cover for a live bug: two prompts seven seconds apart on one
    session produced two overlapping agent runs - two Langfuse traces on the same
    session, with each turn's tool calls interleaved between the other's. Both
    wrote the same fixed-id UI-namespace row, so the transcript ended up with
    duplicated tool calls and whichever reply wrote last.
    """

    def test_warning_is_shown_and_no_bubble_is_appended(self):
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("first question").run()
        thread_id = at.session_state["current_thread_id"]
        before = list(at.session_state["chat_messages"])

        # A turn is in flight for this thread (what an interrupted turn looks like).
        _active_run(thread_id)

        at.chat_input[0].set_value("second question").run()

        warnings = " ".join(el.value for el in at.warning)
        assert "still working" in warnings, f"no busy warning rendered: {warnings!r}"
        # The refused prompt must not appear in the view either: it was never
        # persisted, so showing it would put the session ahead of the checkpointer.
        users = [m.get("content") for m in at.session_state["chat_messages"]
                 if m.get("type") == "user"]
        assert "second question" not in users
        assert [m.get("type") for m in at.session_state["chat_messages"]] == [
            m.get("type") for m in before
        ]

    def test_no_extra_assistant_reply_is_produced(self):
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("first question").run()
        thread_id = at.session_state["current_thread_id"]
        assistants_before = sum(
            1 for m in at.session_state["chat_messages"] if m.get("type") == "assistant"
        )

        _active_run(thread_id)
        at.chat_input[0].set_value("second question").run()

        assistants_after = sum(
            1 for m in at.session_state["chat_messages"] if m.get("type") == "assistant"
        )
        assert assistants_after == assistants_before

    def test_a_turn_is_accepted_again_once_idle(self):
        """The guard must not turn into a permanent lock."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("first question").run()
        thread_id = at.session_state["current_thread_id"]

        _active_run(thread_id)
        at.chat_input[0].set_value("refused").run()

        from streamlit_ui.services.run_registry import REGISTRY

        REGISTRY.get(thread_id).finish()
        at.run()
        at.chat_input[0].set_value("accepted").run()

        users = [m.get("content") for m in at.session_state["chat_messages"]
                 if m.get("type") == "user"]
        assert "accepted" in users


class _ReportWritingRunner(MockAgentRunner):
    """A mock runner whose turn writes a report into the file store.

    The shared app runner has no ``write_file`` call, so a plain turn changes
    nothing in the store and the #30 rerun is (correctly) not triggered. This
    exists to exercise the branch where a turn *does* produce a report.
    """

    def _produce(self, message, thread_id):
        self._store.write_file("reports/turn_report.md", b"# generated during the turn")
        yield from super()._produce(message, thread_id)


def _reports_harness() -> None:
    """Script that calls render_main_chat once; counts its executions."""
    import streamlit as st
    from tests.mock.mock_objects_streamlit import HARNESS

    HARNESS["executions"] = HARNESS.get("executions", 0) + 1
    st.session_state.current_thread_id = "th-reports"
    st.session_state.chat_messages = []
    import streamlit_ui.app as app_module

    app_module.render_main_chat(HARNESS["service"])
    # Rendered after the turn, so the last value the AppTest sees is the one a
    # rerun produced.
    st.text(f"reports={HARNESS['service'].list_reports()}")


class TestReportsSelectorRefreshesAfterATurn:
    """#30: the Reports selector must not be stale once a turn has finished.

    The sidebar renders before the chat input is handled, so it reads the reports
    list from before the turn ran. A report the agent writes during the turn
    therefore cannot appear in the selector unless the script runs again - which
    previously happened only for a brand-new session. The effect was the agent
    narrating "report saved" while the UI still read "No reports available yet",
    with Download disabled at exactly the moment the report existed.
    """

    @staticmethod
    def _build_service():
        from streamlit_ui.services.agent_service import PenTestAgentService
        from tests.mock.mock_objects_streamlit import MockCheckpointer, MockFileStore

        store = MockFileStore()
        store.write_file("reports/pre_existing.md", b"# before")
        cp = MockCheckpointer()
        runner = _ReportWritingRunner(delay_seconds=0.0, checkpointer=cp)
        runner._store = store
        runner.set_final_message("Report saved to /reports/turn_report.md")
        return PenTestAgentService(
            thread_id="th-reports",
            checkpointer=cp,
            file_store=store,
            agent_runner=runner,
        )

    @pytest.fixture(autouse=True)
    def _no_status_fragment(self, monkeypatch):
        """Silence the running-status fragment while counting executions.

        ``render_run_status`` is a ``st.fragment(run_every=1.0)``, so it schedules
        its own script runs and would make an execution count meaningless. The
        fragment's behaviour is covered by TestRunStatusStrip; this class is only
        about the end-of-turn rerun.
        """
        import streamlit_ui.app as app_module

        monkeypatch.setattr(app_module, "render_run_status", lambda service: None)

    def test_a_turn_that_writes_a_report_triggers_a_rerun(self):
        from tests.mock.mock_objects_streamlit import HARNESS

        HARNESS.clear()
        service = self._build_service()
        HARNESS["service"] = service

        at = AppTest.from_function(_reports_harness).run()
        assert HARNESS["executions"] == 1, "harness did not boot"

        at.chat_input[0].set_value("write the report").run()

        # boot + turn + one rerun. The rerun is what re-reads the store, so the
        # Reports selector stops being stale (#30).
        assert HARNESS["executions"] == 3, (
            f"expected exactly one rerun after the turn, got "
            f"{HARNESS['executions'] - 2} rerun(s)"
        )
        reports = service.list_reports()
        assert "turn_report.md" in reports, f"the turn wrote no report: {reports}"
        # And the rerun re-read the store, which is the actual #30 symptom.
        assert any("turn_report.md" in t.value for t in at.text)

    def test_a_turn_that_writes_no_report_does_not_rerun(self):
        """The rerun costs a full script pass (incl. list_threads), so it is gated."""
        from tests.mock.mock_objects_streamlit import HARNESS
        from streamlit_ui.services.agent_service import PenTestAgentService
        from tests.mock.mock_objects_streamlit import MockCheckpointer, MockFileStore

        HARNESS.clear()
        store = MockFileStore()
        store.write_file("reports/pre_existing.md", b"# before")
        cp = MockCheckpointer()
        service = PenTestAgentService(
            thread_id="th-reports",
            checkpointer=cp,
            file_store=store,
            agent_runner=MockAgentRunner(delay_seconds=0.0, checkpointer=cp),
        )
        HARNESS["service"] = service

        at = AppTest.from_function(_reports_harness).run()
        at.chat_input[0].set_value("just answer, write nothing").run()

        # boot + turn, and no third execution: the rerun costs a full script pass
        # (including list_threads, ~0.5s on Postgres) so it is gated on the reports
        # list actually changing.
        assert HARNESS["executions"] == 2, (
            f"reran for a turn that changed nothing in the store "
            f"({HARNESS['executions'] - 1} rerun(s))"
        )


class TestReconcileHelper:
    """``_reconcile_with_persisted`` is extracted so it can be tested directly.

    It used to sit inline in ``main()``, where ``AppTest`` cannot reach it:
    ``AppTest`` executes ``app.py`` in its own globals, so monkeypatching the
    imported module never affects the running script. An earlier version of these
    tests patched ``app_module.render_file_uploader`` to force a sidebar rerun and
    passed - while asserting nothing, because the patched function was never
    called by the script.

    So the helper is called from a harness script instead, which is a real Streamlit
    script run with a real ``st.session_state``. What is verified here is the
    behaviour. The ordering guarantee (reconcile BEFORE the sidebar) is a
    three-line fact at the top of ``main()``, documented on the helper - re-check
    it in review.
    """

    def test_no_thread_is_a_no_op(self):
        holder = {"thread_id": None, "seen": None, "svc": _service("t-none")}
        at = _run_harness(holder)
        assert at.session_state["reads_after_reconcile"] == 0, (
            "touched the checkpointer with no active thread"
        )

    def test_rehydrates_when_the_revision_moved(self):
        thread_id = "t-moved"
        svc = _service(thread_id)
        svc._checkpointer.put(thread_id, {"messages": [{"type": "user", "content": "persisted"}]})
        # A run exists and moved on; the session last saw revision 0.
        from streamlit_ui.services.run_registry import REGISTRY

        REGISTRY.start(thread_id, "q", [{"type": "user", "content": "persisted"}])
        REGISTRY.get(thread_id).record_event({"type": "tool_call", "data": {}})

        at = _run_harness({"thread_id": thread_id, "seen": 0, "svc": svc})
        assert at.session_state["chat_messages"] == [{"type": "user", "content": "persisted"}]
        assert at.session_state["seen_after"] == REGISTRY.revision(thread_id)

    def test_no_read_when_the_revision_unchanged(self):
        """An idle rerun must not re-read the conversation.

        ``get_chat_history`` is a blocking bridge call (~0.5-0.6s on Postgres), so
        gating it on the revision counter is what keeps ordinary widget clicks
        cheap.
        """
        from streamlit_ui.services.run_registry import REGISTRY

        thread_id = "t-quiet"
        svc = _service(thread_id)
        svc._checkpointer.put(thread_id, {"messages": [{"type": "user", "content": "x"}]})
        state = REGISTRY.start(thread_id, "q", [])
        state.finish()
        current = REGISTRY.revision(thread_id)

        at = _run_harness({"thread_id": thread_id, "seen": current, "svc": svc})
        assert at.session_state["reads_after_reconcile"] == 0, (
            "reconciled without a reason - every widget click would pay a DB read"
        )