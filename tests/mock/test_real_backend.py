"""Tests for the real-backend adapters (partial swap: real DB + mock agent).

These exercise the ``streamlit_ui.services.real_backend`` adapters against a
real LangGraph SQLite checkpointer + store, using a temporary DB file per test
so the developer's ``agent/agent_session.db`` is never touched.
"""

import asyncio
import os
import tempfile
import threading
import time
from pathlib import Path

import pytest

from streamlit_ui.services.agent_service import PenTestAgentService
from streamlit_ui.services import real_backend as rb
from streamlit_ui import runtime_status as rs
from tests.mock.mock_objects_streamlit import MockAgentRunner


def _reset_backend_cache():
    rb._CACHE = None


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "agent_test.db")


@pytest.fixture
def backend(db_path: str):
    runner, runtime = rb.create_real_backend(db_path=db_path)
    yield runner, runtime


class TestRealCheckpointerAdapter:
    def test_put_get_roundtrip(self, backend):
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer)
        cp.put("t1", {"messages": [{"role": "user", "content": "hello"}]})
        state = cp.get("t1")
        assert state == {"messages": [{"role": "user", "content": "hello"}]}

    def test_get_unknown_thread_returns_none(self, backend):
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer)
        assert cp.get("missing") is None

    def test_list_threads_dedupes_versions(self, backend):
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer)
        cp.put("t1", {"messages": []})
        cp.put("t1", {"messages": [{"role": "user", "content": "a"}]})  # second version
        cp.put("t2", {"messages": [{"role": "user", "content": "b"}]})
        assert cp.list_threads() == ["t1", "t2"]
        assert cp.get("t1") == {"messages": [{"role": "user", "content": "a"}]}

    def test_delete_existing_and_missing(self, backend):
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer)
        cp.put("t1", {"messages": [{"role": "user", "content": "x"}]})
        assert cp.delete("t1") is True
        assert cp.get("t1") is None
        assert cp.delete("t1") is False

    def test_ui_namespace_isolated_from_agent_namespace(self, backend):
        """UI display history lives in ns ``ui``; agent state in ns ``""``."""
        runner, runtime = backend
        ui_cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer, checkpoint_ns=rb._UI_CHECKPOINT_NS)
        agent_cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer)
        thread_id = "shared"
        agent_cp.put(thread_id, {"messages": [{"role": "user", "content": "agent history"}]})
        ui_cp.put(thread_id, {"messages": [{"role": "user", "content": "ui display"}]})
        assert agent_cp.get(thread_id) == {"messages": [{"role": "user", "content": "agent history"}]}
        assert ui_cp.get(thread_id) == {"messages": [{"role": "user", "content": "ui display"}]}
        assert ui_cp.list_threads() == [thread_id]  # scans all namespaces, dedupes
        assert ui_cp.delete(thread_id) is True  # removes the whole thread
        assert agent_cp.get(thread_id) is None
        assert ui_cp.get(thread_id) is None

    def test_ui_put_with_fixed_id_replaces_in_place(self, backend):
        runner, runtime = backend
        cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer, checkpoint_ns=rb._UI_CHECKPOINT_NS)
        cp.put("t", {"messages": [{"role": "user", "content": "v1"}]})
        cp.put("t", {"messages": [{"role": "user", "content": "v2"}]})
        assert cp.list_threads() == ["t"]  # upsert, not multiply versions
        assert cp.get("t") == {"messages": [{"role": "user", "content": "v2"}]}


class TestRealFileStoreAdapter:
    def test_write_read_roundtrip(self, backend):
        runner, runtime = backend
        fs = rb.RealFileStoreAdapter(runner, runtime.store)
        fs.write_file("findings/nmap.txt", "PORT 80/TCP  open".encode())
        assert fs.read_file("findings/nmap.txt") == b"PORT 80/TCP  open"

    def test_stored_value_uses_v2_agent_format(self, backend):
        writer, runtime = backend
        fs = rb.RealFileStoreAdapter(writer, runtime.store)
        fs.write_file("reports/r.md", b"# Report")
        # write_file stores under the agent's /slash-canonical key...
        item = writer.run(runtime.store.aget(rb._NAMESPACE, "/reports/r.md"))
        assert item is not None
        assert isinstance(item.value, dict)
        assert item.value["content"] == "# Report"
        assert item.value["encoding"] == "utf-8"
        # ...and never under the old slash-less key (the UI bug being fixed).
        legacy = writer.run(runtime.store.aget(rb._NAMESPACE, "reports/r.md"))
        assert legacy is None

    def test_read_missing_returns_none(self, backend):
        runner, runtime = backend
        fs = rb.RealFileStoreAdapter(runner, runtime.store)
        assert fs.read_file("nope.md") is None

    def test_list_files_filters_by_prefix(self, backend):
        runner, runtime = backend
        fs = rb.RealFileStoreAdapter(runner, runtime.store)
        fs.write_file("findings/a.txt", b"a")
        fs.write_file("findings/b.txt", b"b")
        fs.write_file("reports/r.md", b"r")
        assert fs.list_files() == ["findings/a.txt", "findings/b.txt", "reports/r.md"]
        assert fs.list_files("findings/") == ["findings/a.txt", "findings/b.txt"]

    def test_delete_file(self, backend):
        runner, runtime = backend
        fs = rb.RealFileStoreAdapter(runner, runtime.store)
        fs.write_file("findings/a.txt", b"a")
        assert fs.delete_file("findings/a.txt") is True
        assert fs.read_file("findings/a.txt") is None
        assert fs.delete_file("findings/a.txt") is False


class TestPartialSwapService:
    """PenTestAgentService wired to real checkpointer+store but mock agent."""

    def _build(self, thread_id: str, runner, runtime) -> PenTestAgentService:
        agent_runner = MockAgentRunner(delay_seconds=0.0)
        agent_runner.set_tool_calls([{"name": "nmap_scan", "args": {"target": "10.0.0.1"}}])
        return PenTestAgentService(
            thread_id=thread_id,
            checkpointer=rb.RealCheckpointerAdapter(runner, runtime.checkpointer),
            file_store=rb.RealFileStoreAdapter(runner, runtime.store),
            agent_runner=agent_runner,
        )

    def test_chat_history_persists_to_real_db(self, backend):
        runner, runtime = backend
        service = self._build("s1", runner, runtime)
        service.save_chat_history([{"role": "user", "content": "scan"}, {"role": "assistant", "content": "done"}])
        assert service.get_chat_history() == [
            {"role": "user", "content": "scan"},
            {"role": "assistant", "content": "done"},
        ]
        second = self._build("s1", runner, runtime)
        assert second.get_chat_history()[0]["content"] == "scan"

    def test_findings_and_reports_touch_real_store(self, backend):
        runner, runtime = backend
        service = self._build("s1", runner, runtime)
        service.upload_finding("nmap.txt", b"80 open")
        assert service.list_findings() == ["nmap.txt"]
        assert service.list_reports() == []
        assert service.download_report("nmap.txt") is None  # findings not reports

    def test_send_message_uses_mock_agent_events(self, backend):
        runner, runtime = backend
        service = self._build("s1", runner, runtime)
        events = list(service.send_message("scan target"))
        types = [e["type"] for e in events]
        assert "tool_call" in types and "message" in types

    def test_list_and_delete_threads(self, backend):
        runner, runtime = backend
        a = self._build("ta", runner, runtime)
        b = self._build("tb", runner, runtime)
        a.save_chat_history([{"role": "user", "content": "x"}])
        b.save_chat_history([{"role": "user", "content": "y"}])
        assert a.list_threads() == ["ta", "tb"]
        assert a.delete_thread("ta") is True
        assert a.list_threads() == ["tb"]


class TestRealAgentRunnerAdapter:
    """Real agent runner streams LangGraph events into UI-visible events."""

    class _FakeAsyncAgent:
        def __init__(self, chunks, exc=None):
            self._chunks = chunks
            self._exc = exc
            self.calls = []

        async def astream(self, stream_input, config=None, stream_mode=None):
            self.calls.append((stream_input, config, stream_mode))
            if self._exc is not None:
                raise self._exc
            for batch in self._chunks:
                yield batch

    @staticmethod
    def _events(backend, agent, message="scan target", thread_id="th"):
        runner, _runtime = backend
        adapter = rb.RealAgentRunnerAdapter(runner, agent)
        return list(adapter.run(message, thread_id))

    def test_streams_tool_call_then_message(self, backend, monkeypatch):
        """Default node-diff mode drives tool_call/tool_result/message events;
        legacy (message_chunk, metadata) tuples are still tolerated."""
        from langchain_core.messages import AIMessageChunk, ToolMessage

        monkeypatch.delenv("STREAMLIT_AGENT_PRINT", raising=False)
        agent = self._FakeAsyncAgent([
            {"agent": {"messages": [AIMessageChunk(
                content="", tool_calls=[{"name": "nmap_scan", "args": {"target": "10.0.0.1"},
                                         "id": "c1", "type": "tool_call"}])]}},
            {"agent": {"messages": [ToolMessage(content="80/tcp open", tool_call_id="c1", name="nmap_scan")]}},
            (AIMessageChunk(content="Scan"), {"langgraph_node": "agent"}),
            (AIMessageChunk(content=" complete."), {"langgraph_node": "agent"}),
            {"agent": {"messages": [AIMessageChunk(content="Scan complete.")]}},
        ])
        events = self._events(backend, agent, "scan target", "th1")
        assert [e["type"] for e in events] == [
            "tool_call", "tool_result", "token", "token", "message",
        ]
        assert events[0]["data"] == {"name": "nmap_scan", "args": {"target": "10.0.0.1"}}
        assert events[1]["data"]["name"] == "nmap_scan"
        assert [e["data"] for e in events if e["type"] == "token"] == ["Scan", " complete."]
        assert events[-1]["data"] == "Scan complete."
        stream_input, config, stream_mode = agent.calls[0]
        assert stream_input == {"messages": [{"role": "user", "content": "scan target"}]}
        assert stream_mode is None
        assert config["configurable"]["thread_id"] == "th1"
        assert config["configurable"]["checkpoint_ns"] == ""
        assert config["metadata"]["langfuse_session_id"] == "th1"

    def test_handles_top_level_messages_and_serialized_dicts(self, backend, monkeypatch):
        monkeypatch.delenv("STREAMLIT_AGENT_PRINT", raising=False)
        agent = self._FakeAsyncAgent([
            {"messages": [{"role": "assistant", "content": "", "tool_calls": [
                {"name": "nmap_scan", "args": {"target": "10.0.0.1"}, "id": "c1"}]},
                {"role": "tool", "name": "nmap_scan", "content": "80 open", "tool_call_id": "c1"}]},
            {"agent": {"messages": [{"role": "assistant", "content": "Done quickly."}]}},
        ])
        events = self._events(backend, agent)
        assert [e["type"] for e in events] == ["tool_call", "tool_result", "message"]
        assert events[0]["data"] == {"name": "nmap_scan", "args": {"target": "10.0.0.1"}}
        assert events[2]["data"] == "Done quickly."

    def test_multiple_text_chunks_join_into_one_message(self, backend, monkeypatch):
        from langchain_core.messages import AIMessageChunk

        monkeypatch.delenv("STREAMLIT_AGENT_PRINT", raising=False)
        agent = self._FakeAsyncAgent([
            {"messages": [AIMessageChunk(content="Recon ")],
             "extra": [{"type": "whatever"}]},  # non-message keys ignored
            {"messages": [AIMessageChunk(content="complete.")]},
        ])
        events = self._events(backend, agent)
        assert [e["type"] for e in events] == ["message"]
        assert events[0]["data"] == "Recon complete."

    def test_stream_error_surfaces_error_event(self, backend, monkeypatch):
        monkeypatch.delenv("STREAMLIT_AGENT_PRINT", raising=False)
        agent = self._FakeAsyncAgent([], exc=ValueError("llm exploded"))
        events = self._events(backend, agent)
        assert events == [{"type": "error", "data": "ValueError: llm exploded"}]

    def test_print_enabled_emits_marker_lines(self, backend, monkeypatch, capsys):
        from langchain_core.messages import AIMessageChunk, ToolMessage

        monkeypatch.setenv("STREAMLIT_AGENT_PRINT", "1")
        agent = self._FakeAsyncAgent([
            {"messages": [AIMessageChunk(
                content="", tool_calls=[{"name": "read_file", "args": {"path": "/a/b.md"},
                                         "id": "c1", "type": "tool_call"}])]},
            {"messages": [ToolMessage(content="# Markdown body", tool_call_id="c1", name="read_file")]},
            {"messages": [AIMessageChunk(content="Done here.")]},
        ])
        self._events(backend, agent)
        out = capsys.readouterr().out
        assert "[TOOL CALL]: read_file" in out
        assert "[TOOL RESULT]: read_file" in out
        assert "[CNK]: Done here." in out
        assert "[AGENT RESPONSE]: Done here." in out


def _wait_for_run(registry, thread_id, timeout=5.0):
    """Wait for a registered run to exist and be in flight."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if registry.get(thread_id) is not None and registry.is_running(thread_id):
            return True
        time.sleep(0.02)
    return False


def _wait_until_idle(registry, thread_id, timeout=5.0):
    """Wait for a run to reach a terminal state."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not registry.is_running(thread_id):
            return True
        time.sleep(0.02)
    return False


class TestRunCancellation:
    """Stopping a turn must actually stop the agent stream (issue #39).

    ## Why these tests changed shape

    This originally asserted that *closing the generator* stops the agent - the
    `Future` from `schedule()` was discarded, so nothing cancelled the coroutine
    and the agent kept streaming to completion after the UI moved on. That fix is
    incompatible with #20, and not because of a subtlety in this code: because of
    what Streamlit 1.62 actually raises.

    With ``runner.fastReruns`` on (the default), a widget click does not raise
    ``RerunException``. ``AppSession.request_rerun`` takes the fast path and calls
    ``request_stop()`` (``app_session.py:503``), so the running script raises
    ``StopException`` - the same exception the browser's Stop button produces via
    ``request_script_stop`` (``app_session.py:518-524``).

    So closing the generator cannot mean "cancel": doing so would kill every turn
    the instant a sidebar widget was touched, which is #20 all over again.
    Cancellation is now explicit (``adapter.cancel(thread_id)``), and the UI calls
    it from a button. The semantics #39 established are preserved - a stopped
    turn emits no final message - and that is what these tests now pin.
    """

    class _EndlessAgent:
        """Emits one tool call, then blocks forever - like a long agent turn.

        The first update is a tool call rather than plain text because plain
        content only accumulates into final_parts and emits no event, so a
        consumer waiting on the generator would block before reaching the part
        of the stream this test is about.
        """

        def __init__(self):
            from langchain_core.messages import AIMessageChunk

            self._chunk = AIMessageChunk(
                content="",
                tool_calls=[{"name": "ls", "args": {"path": "/context"}, "id": "c1",
                             "type": "tool_call"}],
            )
            self.started = threading.Event()
            self.stopped = False

        async def astream(self, stream_input, config=None, stream_mode=None):
            self.started.set()
            try:
                yield {"messages": [self._chunk]}
                while True:
                    await asyncio.sleep(0.01)
                    # Keep emitting, so a cooperative stop at the top of the
                    # consumer's update loop has updates to notice. A generator
                    # that yields once and then blocks would never be re-entered.
                    yield {"messages": [self._chunk]}
            except asyncio.CancelledError:
                self.stopped = True
                raise

    def _drain_until_stopped(self, agent, timeout=5.0):
        deadline = time.monotonic() + timeout
        while not agent.stopped and time.monotonic() < deadline:
            time.sleep(0.02)
        return agent.stopped

    def test_cancel_stops_the_agent_stream(self, backend, monkeypatch):
        monkeypatch.delenv("STREAMLIT_AGENT_PRINT", raising=False)
        runner, _runtime = backend
        agent = self._EndlessAgent()
        adapter = rb.RealAgentRunnerAdapter(runner, agent)
        from streamlit_ui.services.run_registry import REGISTRY

        REGISTRY.clear()
        adapter.start("long turn", "th-cancel")
        assert _wait_for_run(REGISTRY, "th-cancel")
        assert agent.started.wait(5)

        assert adapter.cancel("th-cancel") is True

        assert self._drain_until_stopped(agent), (
            "the agent stream kept running after cancel()"
        )
        assert _wait_until_idle(REGISTRY, "th-cancel")

    def test_abandoning_the_consumer_does_not_stop_the_run(self, backend, monkeypatch):
        """The #20 guarantee, pinned directly.

        Closing the consumer is what a widget click does to the script run. If that
        stopped the agent, every interrupted turn would be lost again - so this is
        the test that would catch a regression back toward #39's original approach.
        """
        monkeypatch.delenv("STREAMLIT_AGENT_PRINT", raising=False)
        runner, _runtime = backend
        agent = self._EndlessAgent()
        adapter = rb.RealAgentRunnerAdapter(runner, agent)
        from streamlit_ui.services.run_registry import REGISTRY

        REGISTRY.clear()
        adapter.start("long turn", "th-abandon")
        assert _wait_for_run(REGISTRY, "th-abandon")
        assert agent.started.wait(5)

        # No consumer was ever attached - the run is entirely unattended.
        time.sleep(0.2)

        assert not agent.stopped, "an unattended run was cancelled"
        assert REGISTRY.is_running("th-abandon"), "an unattended run was cancelled"
        adapter.cancel("th-abandon")  # tidy up
        assert self._drain_until_stopped(agent)

    def test_cancelled_run_emits_no_final_message(self, backend, monkeypatch, capsys):
        """A stopped turn must not be reported as a finished one.

        The finally block emits whatever text had accumulated and can fall back to
        the checkpointer. Doing either writes state for a turn the user stopped,
        which then appears in the session list as a completed entry.
        """
        monkeypatch.setenv("STREAMLIT_AGENT_PRINT", "1")
        runner, _runtime = backend
        agent = self._EndlessAgent()
        adapter = rb.RealAgentRunnerAdapter(runner, agent)
        from streamlit_ui.services.run_registry import REGISTRY

        REGISTRY.clear()
        adapter.start("long turn", "th-cancel2")
        assert _wait_for_run(REGISTRY, "th-cancel2")
        assert agent.started.wait(5)

        adapter.cancel("th-cancel2")
        assert self._drain_until_stopped(agent)
        assert _wait_until_idle(REGISTRY, "th-cancel2")

        out = capsys.readouterr().out
        # The handle cancellation arrives as CancelledError at the agent's own
        # await point, so "cancelled" is what gets logged - the cooperative
        # "stop requested by the user" check only fires when the cancel happens to
        # land on a graph-update boundary instead.
        assert "cancelled at" in out
        assert "skipping final message for a stopped run" in out
        assert "[AGENT RESPONSE]" not in out
        assert "AGENT RESPONSE (checkpointer)" not in out

    def test_cancel_is_refused_when_nothing_is_running(self, backend):
        runner, _runtime = backend
        adapter = rb.RealAgentRunnerAdapter(runner, self._EndlessAgent())
        from streamlit_ui.services.run_registry import REGISTRY

        REGISTRY.clear()
        assert adapter.cancel("never-started") is False

    def test_normal_completion_is_unaffected(self, backend, monkeypatch):
        monkeypatch.delenv("STREAMLIT_AGENT_PRINT", raising=False)
        from langchain_core.messages import AIMessageChunk

        agent = TestRealAgentRunnerAdapter._FakeAsyncAgent([
            {"messages": [AIMessageChunk(content="all done")]},
        ])
        runner, _runtime = backend
        adapter = rb.RealAgentRunnerAdapter(runner, agent)
        events = list(adapter.run("quick", "th-ok"))
        assert [e["type"] for e in events] == ["message"]
        assert events[0]["data"] == "all done"


class TestRealBackendStatus:
    def test_real_backend_lines_connect_sqlite(self, monkeypatch, tmp_path):
        monkeypatch.delenv("STREAMLIT_USE_MOCKS", raising=False)
        monkeypatch.setenv("STREAMLIT_USE_REAL_BACKEND", "1")
        monkeypatch.setenv("REPORT_AGENT_DB_PATH", str(tmp_path / "status_probe.db"))
        _reset_backend_cache()
        try:
            lines = rs.component_status_lines()
        finally:
            _reset_backend_cache()
        assert len(lines) == 5
        assert lines[0].startswith("checkpointer: connected (sqlite:")
        assert lines[1].startswith("virtual filesystem store: connected (sqlite:")
        assert "llm api: not connected" in lines[2]
        assert "agent object: not connected" in lines[3]
        assert "observability (langfuse):" in lines[4]

    def test_mock_mode_still_wins_over_real_backend(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        monkeypatch.setenv("STREAMLIT_USE_REAL_BACKEND", "1")
        lines = rs.component_status_lines()
        assert lines[0] == "checkpointer: connected (mock, in-memory)"


class TestRealBackendAppTest:
    """App-level smoke test: the app must boot in real-backend mode."""

    def test_app_loads_without_errors_and_no_session_created(self, monkeypatch, tmp_path):
        from streamlit.testing.v1 import AppTest
        from pathlib import Path as _Path

        APP_PATH = str(_Path(__file__).resolve().parent.parent.parent / "streamlit_ui" / "app.py")
        monkeypatch.delenv("STREAMLIT_USE_MOCKS", raising=False)
        monkeypatch.setenv("STREAMLIT_USE_REAL_BACKEND", "1")
        monkeypatch.setenv("REPORT_AGENT_DB_PATH", str(tmp_path / "apptest.db"))
        _reset_backend_cache()
        try:
            at = AppTest.from_file(APP_PATH)
            at.run(timeout=60)  # backend init retries Postgres + Langfuse checks
            assert not at.exception, f"App failed to load: {at.exception}"
            assert "current_thread_id" in at.session_state
            assert at.session_state.current_thread_id is None
        finally:
            _reset_backend_cache()