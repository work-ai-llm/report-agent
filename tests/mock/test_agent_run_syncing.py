"""Tests for rerun-surviving agent runs (issue #20).

## The bug these lock down

A Streamlit script run is linear. Any widget interaction queues a rerun, and the
queued rerun unwinds the current script mid-execution - the browser drops the
elements already pushed, and the Python stack unwinds without warning.

The agent turn was never affected: ``RealAgentRunnerAdapter`` schedules the
LangGraph stream onto the persistent ``EventLoopThread`` and returns immediately.
So the run kept going and kept checkpointing into the agent's namespace after the
UI had forgotten about it. Worse, the UI-namespace conversation was written by the
*script* thread (in a ``finally``), so a killed script left the two histories
permanently diverged and the turn was gone from the UI forever.

The fix moves ownership: the run lives in ``run_registry.REGISTRY`` and persists
itself from the event-loop thread. These tests assert that ownership, not the UI.

The centrepiece is
:meth:`TestRunSurvivesAbandonedConsumer.test_persisted_turn_is_complete_when_nobody_ever_read_it`
- it reproduces the original failure by never consuming the stream at all, and
passes only because persistence no longer depends on a consumer.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from streamlit_ui.services import real_backend as rb
from streamlit_ui.services.agent_service import PenTestAgentService
from streamlit_ui.services.run_registry import DONE, ERROR, RUNNING, REGISTRY, RunRegistry, RunState
from tests.mock.mock_objects_streamlit import MockAgentRunner, MockCheckpointer


@pytest.fixture(autouse=True)
def _clean_registry():
    """The registry is a process singleton; isolate every test from the last."""
    REGISTRY.clear()
    yield
    REGISTRY.clear()


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "syncing_test.db")


@pytest.fixture
def backend(db_path: str):
    runner, runtime = rb.create_real_backend(db_path=db_path)
    yield runner, runtime
    rb._CACHE = None


def _wait_for(predicate, timeout: float = 15.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


class _SlowAgent:
    """Fake agent that yields tool calls, then waits, then yields the answer.

    ``gap_seconds`` between the tool call and the final message is the window in
    which a real Streamlit rerun would abandon the script thread. The run must
    complete anyway.
    """

    def __init__(self, tool_names=("ls", "read_file"), gap_seconds: float = 0.3):
        self._tool_names = list(tool_names)
        self._gap = gap_seconds
        self.astream_calls = 0

    async def astream(self, stream_input, config=None, stream_mode=None):
        from langchain_core.messages import AIMessage, ToolMessage

        self.astream_calls += 1
        for name in self._tool_names:
            yield {"agent": {"messages": [AIMessage(
                content="",
                tool_calls=[{"name": name, "args": {"p": name}, "id": f"c_{name}", "type": "tool_call"}],
            )]}}
            yield {"tools": {"messages": [ToolMessage(content=f"{name}-ok", tool_call_id=f"c_{name}", name=name)]}}
        # The gap a rerun would interrupt.
        import asyncio

        await asyncio.sleep(self._gap)
        yield {"agent": {"messages": [AIMessage(content="All done.")]}}

    async def aget_state(self, config):
        class _S:
            values: dict = {}
        return _S()


class TestRunSurvivesAbandonedConsumer:
    """The core regression: no consumer, no loss."""

    def test_persisted_turn_is_complete_when_nobody_ever_read_it(self, backend):
        """The whole point: persistence must not depend on a live consumer.

        This is the #20 failure mode. Previously the script thread owned the
        write; here we abandon the consumer entirely - never call poll(), never
        iterate run() - and still expect the full transcript on disk.
        """
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer, checkpoint_ns=rb._UI_CHECKPOINT_NS)
        agent = _SlowAgent(gap_seconds=0.3)
        adapter = rb.RealAgentRunnerAdapter(runner, agent, checkpointer=cp)

        adapter.start("scan the box", "th-abandoned")

        # Nobody consumes. Wait for the run to finish on its own.
        assert _wait_for(lambda: not REGISTRY.is_running("th-abandoned")), "run never completed"
        # Let the final persist land.
        assert _wait_for(
            lambda: any(
                m.get("type") == "assistant"
                for m in (cp.get("th-abandoned") or {}).get("messages", [])
            )
        ), "assistant reply never reached the checkpointer"

        rows = cp.get("th-abandoned")["messages"]
        types = [m.get("type") for m in rows]
        assert types[0] == "user"
        assert rows[0]["content"] == "scan the box"
        assert types[-1] == "assistant"
        assert rows[-1]["content"] == "All done."
        assert types.count("tool_call") == 2

    def test_user_message_is_durable_before_the_first_event(self, backend):
        """The prompt must survive even if the run dies immediately.

        Before the fix, the user message was only written as a side effect of the
        first streamed event, so a turn that produced no events wrote nothing.
        """
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer, checkpoint_ns=rb._UI_CHECKPOINT_NS)

        class _NeverYields:
            async def astream(self, *a, **k):
                if False:
                    yield {}
                import asyncio

                await asyncio.sleep(30)

            async def aget_state(self, config):
                class _S:
                    values: dict = {}
                return _S()

        adapter = rb.RealAgentRunnerAdapter(runner, _NeverYields(), checkpointer=cp)
        adapter.start("hello", "th-early")

        rows = cp.get("th-early")["messages"]
        assert rows == [{"type": "user", "content": "hello"}]

    def test_run_continues_after_the_consumer_stops_polling(self, backend):
        """Abandoning the consumer mid-stream does not cancel the run."""
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer, checkpoint_ns=rb._UI_CHECKPOINT_NS)
        agent = _SlowAgent(gap_seconds=0.25)
        adapter = rb.RealAgentRunnerAdapter(runner, agent, checkpointer=cp)

        adapter.start("go", "th-midstream")
        # Read a couple of events, then walk away mid-turn.
        index = 0
        for _ in range(2):
            events, index, _rev = adapter.poll("th-midstream", index)
            if not events:
                break
        assert REGISTRY.is_running("th-midstream")

        assert _wait_for(lambda: not REGISTRY.is_running("th-midstream"))
        rows = cp.get("th-midstream")["messages"]
        assert rows[-1]["content"] == "All done."
        assert agent.astream_calls == 1


class TestPersistenceHappensOnTheLoopThread:
    def test_aput_state_is_awaitable_from_inside_the_loop(self, backend):
        """`aput_state` must not re-enter the blocking bridge.

        Calling `put()` from the loop thread would call `EventLoopThread.run`,
        which blocks the loop on a future only the loop can complete - a hard
        deadlock. If this test hangs, that regressed.
        """
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer, checkpoint_ns=rb._UI_CHECKPOINT_NS)

        async def _from_loop():
            await cp.aput_state("th-loop", {"messages": [{"type": "user", "content": "from-loop"}]})

        # Bounded so a deadlock fails the test instead of hanging the suite.
        runner.run(_from_loop(), timeout=10.0)
        assert cp.get("th-loop")["messages"] == [{"type": "user", "content": "from-loop"}]

    def test_put_and_aput_state_agree(self, backend):
        """Both entry points must produce the same row (same blob-version rules)."""
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer, checkpoint_ns=rb._UI_CHECKPOINT_NS)
        cp.put("t-sync", {"messages": [{"type": "user", "content": "one"}]})

        async def _second():
            await cp.aput_state("t-sync", {"messages": [{"type": "user", "content": "two"}]})

        runner.run(_second(), timeout=10.0)
        assert cp.get("t-sync")["messages"] == [{"type": "user", "content": "two"}]

    def test_blob_version_still_increases_across_mixed_writes(self, backend):
        """The 'messages disappear' bug guard must survive the refactor.

        Reusing a version silently keeps the OLD blob (upsert is ON CONFLICT DO
        NOTHING), so the version must keep climbing when writes alternate between
        the sync and async entry points.
        """
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer, checkpoint_ns=rb._UI_CHECKPOINT_NS)
        cp.put("t-ver", {"messages": [{"type": "user", "content": "a"}]})

        async def _bump():
            await cp.aput_state("t-ver", {"messages": [{"type": "user", "content": "b"}]})

        runner.run(_bump(), timeout=10.0)
        cp.put("t-ver", {"messages": [{"type": "user", "content": "c"}]})
        assert cp.get("t-ver")["messages"] == [{"type": "user", "content": "c"}]


class TestRunStateAndRegistry:
    def test_revision_is_monotonic_and_bumps_on_finish(self):
        reg = RunRegistry()
        state = reg.start("t", "hi")
        assert state.revision == 0
        state.record_event({"type": "tool_call", "data": {}}, [{"type": "tool_call", "name": "ls", "args": {}}])
        assert state.revision == 1
        state.finish()
        assert state.revision == 2
        assert state.status == DONE
        assert not state.is_running

    def test_finish_is_idempotent(self):
        state = RunState("t", "hi")
        state.finish()
        first = state.revision
        state.finish()
        assert state.revision == first

    def test_finish_with_error_marks_error(self):
        state = RunState("t", "hi")
        state.finish(error="boom")
        assert state.status == ERROR
        assert state.error == "boom"

    def test_events_since_returns_only_new_events(self):
        state = RunState("t", "hi")
        state.record_event({"type": "a", "data": 1})
        state.record_event({"type": "b", "data": 2})
        events, nxt = state.events_since(0)
        assert [e["type"] for e in events] == ["a", "b"]
        assert nxt == 2
        events, nxt = state.events_since(1)
        assert [e["type"] for e in events] == ["b"]

    def test_latest_run_per_thread_replaces_previous(self):
        reg = RunRegistry()
        first = reg.start("t", "one")
        second = reg.start("t", "two")
        assert reg.get("t") is second
        assert first is not second

    def test_registry_reports_running_threads_and_drops(self):
        reg = RunRegistry()
        a = reg.start("a", "x")
        reg.start("b", "y")
        assert reg.running_threads() == ["a", "b"]
        b = reg.get("b")
        b.finish()
        assert reg.running_threads() == ["a"]
        reg.drop("a")
        assert reg.get("a") is None

    def test_revision_is_zero_for_unknown_thread(self):
        assert RunRegistry().revision("never-ran") == 0

    def test_token_events_do_not_grow_the_persisted_list(self):
        """Tokens are display-only; persisting each would explode the row count."""
        state = RunState("t", "hi")
        state.record_event({"type": "token", "data": "he"})
        state.record_event({"type": "token", "data": "llo"})
        assert state.messages == []
        assert len(state.events) == 2

    def test_snapshot_is_consistent(self):
        state = RunState("t", "hi", [{"type": "user", "content": "hi"}])
        state.record_event({"type": "message", "data": "yo"}, [{"type": "assistant", "content": "yo"}])
        snap = state.snapshot()
        assert snap["status"] == RUNNING
        assert snap["messages"] == [
            {"type": "user", "content": "hi"},
            {"type": "assistant", "content": "yo"},
        ]


class TestServiceRunState:
    def _service(self):
        cp = MockCheckpointer()
        runner = MockAgentRunner(checkpointer=cp)
        runner.set_tool_calls([{"name": "ls", "args": {"path": "/"}}])
        return PenTestAgentService(
            thread_id="svc", checkpointer=cp, file_store=__import__(
                "tests.mock.mock_objects_streamlit", fromlist=["MockFileStore"]
            ).MockFileStore(), agent_runner=runner,
        ), cp, runner

    def test_send_message_persists_via_the_registry(self):
        service, cp, _runner = self._service()
        list(service.send_message("hello"))
        rows = cp.get("svc")["messages"]
        assert [r["type"] for r in rows] == ["user", "tool_call", "assistant"]
        assert rows[-1]["content"] == "This is a mock agent response."

    def test_run_revision_tracks_progress_and_completion(self):
        service, _cp, _runner = self._service()
        assert service.run_revision() == 0
        assert service.is_run_active() is False
        list(service.send_message("hello"))
        # user message is seeded outside the event stream, then one revision per
        # recorded event (tool_call, message) plus one for finish().
        assert service.run_revision() == 3
        assert service.is_run_active() is False

    def test_active_run_snapshot_reports_terminal_status(self):
        service, _cp, _runner = self._service()
        list(service.send_message("hello"))
        snap = service.active_run_snapshot()
        assert snap["status"] == DONE
        assert snap["error"] is None

    def test_delete_thread_drops_the_run_entry(self):
        service, cp, _runner = self._service()
        list(service.send_message("hello"))
        assert REGISTRY.get("svc") is not None
        assert service.delete_thread("svc") is True
        assert REGISTRY.get("svc") is None

    def test_runner_without_checkpointer_falls_back_to_plain_generator(self):
        """A two-arg MockAgentRunner keeps the old in-process behaviour."""
        runner = MockAgentRunner()
        assert runner.supports_polling() is False
        service = PenTestAgentService(
            thread_id="plain",
            checkpointer=MockCheckpointer(),
            file_store=__import__(
                "tests.mock.mock_objects_streamlit", fromlist=["MockFileStore"]
            ).MockFileStore(),
            agent_runner=runner,
        )
        events = list(service.send_message("hi"))
        assert [e["type"] for e in events] == ["message"]


class TestPartialSwapAdapter:
    def test_partial_mode_run_is_tracked_and_persisted(self, backend):
        """Real persistence + mock agent still records through the registry."""
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer, checkpoint_ns=rb._UI_CHECKPOINT_NS)
        store = rb.RealFileStoreAdapter(runner, runtime.store)
        inner = MockAgentRunner(delay_seconds=0.0)
        inner.set_tool_calls([{"name": "write_file", "args": {"path": "/reports/r.md", "content": "# hi"}}])
        adapter = rb.PersistingMockAdapter(inner, cp, store)

        assert adapter.supports_polling() is True
        adapter.start("make a report", "th-partial")
        assert _wait_for(lambda: not REGISTRY.is_running("th-partial"))

        rows = cp.get("th-partial")["messages"]
        # Partial mode keeps its historical row shape; only the write timing moved.
        assert rows[0] == {"role": "user", "content": "make a report"}
        assert rows[-1]["role"] == "assistant"
        # Mock tool result still lands in the virtual filesystem.
        assert store.read_file("reports/r.md") == b"# hi"

    def test_blocking_run_facade_yields_the_same_events(self, backend):
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer, checkpoint_ns=rb._UI_CHECKPOINT_NS)
        store = rb.RealFileStoreAdapter(runner, runtime.store)
        inner = MockAgentRunner(delay_seconds=0.0)
        inner.set_tool_calls([{"name": "ls", "args": {"path": "/"}}])
        adapter = rb.PersistingMockAdapter(inner, cp, store)
        events = list(adapter.run("go", "th-facade"))
        assert [e["type"] for e in events] == ["tool_call", "message"]