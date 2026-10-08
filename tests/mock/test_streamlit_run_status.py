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
        """Silence the live-turn fragment while counting executions.

        ``render_live_turn`` is a ``st.fragment(run_every=...)``, so it schedules
        its own script runs and would make an execution count meaningless. Its
        behaviour is covered by TestRunStatusStrip; this class is only about the
        end-of-turn rerun.
        """
        import streamlit_ui.app as app_module

        monkeypatch.setattr(app_module, "render_live_turn", lambda service: None)

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


class TestNoDuplicateToolCallsWhileStreaming:
    """A tool call must be drawn once, not once per renderer.

    Observed live: clicking anything mid-turn produced two identical
    ``Calling tool: read_file`` blocks, which corrected themselves when the turn
    finished. Not a checkpointer problem - the stored row had the call exactly once.
    It was two renderers drawing the same rows:

    - the run persists its rows as events arrive, so a widget interaction made
      reconciliation pull the turn's *partial* rows into ``chat_messages``;
    - the normal history render drew them, and the live fragment drew them again.

    So while a run is live the live fragment owns the whole conversation and the
    normal history render is suppressed.

    Reproduced through a harness with a real checkpointer rather than through the
    app's own cached mocks, because the duplication needs the partial rows to be
    *persisted* - reconciliation reads them back from the checkpointer, and an empty
    one would make the test pass whether or not the bug exists. (An earlier version
    of this test passed with the fix disabled, which is how that was found.)
    """

    ROWS = [
        {"type": "user", "content": "read test 2"},
        {"type": "tool_call", "name": "read_file",
         "args": {"file_path": "/context/findings/test_2.txt"},
         "result": "small test file"},
    ]

    @staticmethod
    def _harness():
        """Script: a live run whose rows are already persisted, then the chat area."""
        import streamlit as st

        from tests.mock.mock_objects_streamlit import HARNESS
        from streamlit_ui.services.run_registry import REGISTRY

        service = HARNESS["service"]
        thread_id = HARNESS["thread_id"]

        # The in-flight turn, already persisted - this is what a widget click
        # mid-turn leaves behind.
        service._checkpointer.put(thread_id, {"messages": HARNESS["rows"]})

        st.session_state.current_thread_id = thread_id
        st.session_state._service_thread = thread_id
        if HARNESS["active"]:
            # A stale revision, so reconciliation rehydrates chat_messages from the
            # persisted rows - the exact state a mid-turn interaction creates.
            st.session_state._seen_run_revision = None
            st.session_state.chat_messages = []
        else:
            # Idle: the normal history render owns the transcript, so the rows have
            # to be in chat_messages for it to draw anything.
            st.session_state._seen_run_revision = REGISTRY.revision(thread_id)
            st.session_state.chat_messages = list(HARNESS["rows"])

        import streamlit_ui.app as app_module

        # main()'s order: reconcile first, then render the chat area. The
        # reconciliation is the step that creates the duplication - without it the
        # history render has nothing to draw and the test passes whether or not the
        # bug exists.
        app_module._reconcile_with_persisted(service)
        app_module.render_main_chat(service)

    def _drive(self, run_active: bool):
        from streamlit_ui.services.agent_service import PenTestAgentService
        from streamlit_ui.services.run_registry import REGISTRY
        from tests.mock.mock_objects_streamlit import (
            HARNESS,
            MockAgentRunner,
            MockCheckpointer,
            MockFileStore,
        )

        cp = MockCheckpointer()
        runner = MockAgentRunner(delay_seconds=0.0, checkpointer=cp)
        service = PenTestAgentService(
            thread_id="th-dup",
            checkpointer=cp,
            file_store=MockFileStore(),
            agent_runner=runner,
        )

        # Register the run the fragment will read, sharing the same message list
        # the harness persists.
        state = REGISTRY.start("th-dup", "read test 2", list(self.ROWS))
        state.record_event({"type": "tool_call",
                            "data": {"name": "read_file", "args": {}}})
        if not run_active:
            state.finish()

        HARNESS.clear()
        HARNESS.update({
            "service": service,
            "thread_id": "th-dup",
            "rows": list(self.ROWS),
            "active": run_active,
        })
        return AppTest.from_function(self._harness).run()

    def test_a_mid_turn_interaction_does_not_double_render(self):
        at = self._drive(run_active=True)
        labels = " ".join(str(e.label) for e in at.expander)
        assert labels.count("read_file") == 1, (
            f"the tool call was rendered more than once: {labels!r}"
        )

    def test_history_renders_once_when_idle(self):
        at = self._drive(run_active=False)
        labels = " ".join(str(e.label) for e in at.expander)
        assert labels.count("read_file") == 1, (
            f"rendered {labels.count('read_file')} times when idle: {labels!r}"
        )

    def test_finished_history_is_not_hidden_while_a_run_is_live(self):
        """Suppressing the history render must not hide the earlier turns."""
        from streamlit_ui.services.run_registry import REGISTRY, RunState

        at = AppTest.from_file(APP_PATH).run()
        thread_id = "th-earlier"
        at.session_state["current_thread_id"] = thread_id

        rows = [
            {"type": "user", "content": "first question"},
            {"type": "assistant", "content": "first answer"},
            {"type": "user", "content": "second question"},
        ]
        state = RunState(thread_id, "second question", rows)
        state.record_event({"type": "tool_call", "data": {"name": "ls", "args": {}}})
        REGISTRY.start(thread_id, "second question", rows)
        REGISTRY.get(thread_id).record_event({"type": "tool_call",
                                             "data": {"name": "ls", "args": {}}})

        at.session_state["_seen_run_revision"] = None
        at.session_state["chat_messages"] = []
        at.run()

        text = " ".join(str(getattr(m, "value", m)) for m in at.markdown)
        assert "first answer" in text, (
            f"the earlier turn vanished while a run was live: {text!r}"
        )


class TestTokenStreamRendersWhileRunning:
    """Streamed assistant text must be visible before the turn finishes.

    The run coalesces token deltas into ``in_flight_text`` and clears it when the
    ``message`` event carries the finished answer. Both halves matter:

    - live text has to be rendered from that buffer, not from the token events -
      the event log has one entry per delta, and the history renderer's fallback
      branch turns anything unrecognised into its own chat bubble, so a
      per-event render would emit one bubble per token;
    - the buffer has to be cleared by the final message, or the finished answer
      is drawn twice: once live and once from the persisted transcript.

    Driven through a real checkpointer because reconciliation reads the persisted
    rows back, and the finished-turn case needs them to actually be there.
    """

    ROWS = [{"type": "user", "content": "scan the host"}]
    ANSWER = "Scanned. One host up."

    @staticmethod
    def _harness():
        """Script: a live run mid-token, then the chat area."""
        import streamlit as st

        from streamlit_ui.services.run_registry import REGISTRY
        from tests.mock.mock_objects_streamlit import HARNESS

        service = HARNESS["service"]
        thread_id = HARNESS["thread_id"]

        # Persist the finished rows the transcript should end up holding.
        service._checkpointer.put(thread_id, {"messages": list(HARNESS["rows"])})

        st.session_state.current_thread_id = thread_id
        st.session_state._service_thread = thread_id
        # Stale revision, so reconciliation rehydrates - the state a mid-turn
        # interaction creates.
        st.session_state._seen_run_revision = None
        st.session_state.chat_messages = []

        # Seeded with the history from *before* this turn, as the real adapter
        # does. Seeding it with the answer as well would put the assistant row in
        # the run twice, once from the seed and once from the final message.
        state = REGISTRY.start(thread_id, "scan the host", list(HARNESS["pre_rows"]))
        for delta in HARNESS["deltas"]:
            state.record_event({"type": "token", "data": delta})
        if HARNESS["finished"]:
            state.record_event({"type": "message", "data": HARNESS["answer"]},
                               [{"type": "assistant", "content": HARNESS["answer"]}])
            state.finish()

        import streamlit_ui.app as app_module

        app_module._reconcile_with_persisted(service)
        app_module.render_main_chat(service)

    def _drive(self, deltas, finished: bool):
        from streamlit_ui.services.agent_service import PenTestAgentService
        from tests.mock.mock_objects_streamlit import (
            HARNESS,
            MockAgentRunner,
            MockCheckpointer,
            MockFileStore,
        )

        rows = list(self.ROWS)
        if finished:
            rows.append({"type": "assistant", "content": self.ANSWER})

        service = PenTestAgentService(
            thread_id="th-token",
            checkpointer=MockCheckpointer(),
            file_store=MockFileStore(),
            agent_runner=MockAgentRunner(),
        )
        HARNESS.clear()
        HARNESS.update({
            "service": service,
            "thread_id": "th-token",
            "rows": rows,
            "pre_rows": list(self.ROWS),
            "deltas": list(deltas),
            "finished": finished,
            "answer": self.ANSWER,
        })
        return AppTest.from_function(self._harness).run()

    def test_partial_tokens_are_visible_while_the_turn_runs(self):
        at = self._drive(["Scanned", ". One host up."], finished=False)
        text = " ".join(str(m.value) for m in at.markdown)
        assert "Scanned" in text, f"streamed text not rendered: {text!r}"

    def test_finished_answer_is_not_drawn_twice(self):
        """One answer, drawn by one renderer.

        A finished run hands rendering back to the normal history path, so the
        live region must not keep claiming the text as well. The buffer-clearing
        invariant itself is unit-tested against ``RunState`` - here the live
        region is not rendered at all once the run is terminal.
        """
        at = self._drive(["Scanned", ". One host up."], finished=True)
        text = " ".join(str(m.value) for m in at.markdown)
        assert text.count("Scanned") == 1, (
            f"the answer was rendered more than once: {text!r}"
        )


class TestStoredHistoryNormalisation:
    """Partial mode stores tool calls in a role-shaped row; they must still render."""

    def test_role_shaped_tool_call_becomes_a_tool_call_entry(self):
        from streamlit_ui import app as app_module

        out = app_module._history_from_stored([
            {"role": "user", "content": "read it"},
            {"role": "assistant", "content": None,
             "tool_calls": [{"name": "read_file", "args": {"path": "/a"}}]},
        ])
        assert out == [
            {"type": "user", "content": "read it"},
            {"type": "tool_call", "name": "read_file", "args": {"path": "/a"}},
        ], out

    def test_a_tool_call_row_keeps_a_following_text_answer(self):
        from streamlit_ui import app as app_module

        out = app_module._history_from_stored([
            {"role": "assistant", "content": "here it is",
             "tool_calls": [{"name": "read_file", "args": {}}]},
        ])
        assert [e["type"] for e in out] == ["tool_call", "assistant"]

    def test_a_plain_assistant_row_is_unchanged(self):
        from streamlit_ui import app as app_module

        out = app_module._history_from_stored([{"role": "assistant", "content": "hi"}])
        assert out == [{"type": "assistant", "content": "hi"}]

    def test_structured_rows_pass_through_untouched(self):
        from streamlit_ui import app as app_module

        rows = [{"type": "user", "content": "x"},
                {"type": "tool_call", "name": "ls", "args": {}, "result": "y"}]
        assert app_module._history_from_stored(rows) == rows


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