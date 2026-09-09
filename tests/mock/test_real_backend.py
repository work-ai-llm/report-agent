"""Tests for the real-backend adapters (partial swap: real DB + mock agent).

These exercise the ``streamlit_ui.services.real_backend`` adapters against a
real LangGraph SQLite checkpointer + store, using a temporary DB file per test
so the developer's ``agent/agent_session.db`` is never touched.
"""

import os
import tempfile
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
        runner, runtime = backend
        fs = rb.RealFileStoreAdapter(runner, runtime.store)
        fs.write_file("reports/r.md", b"# Report")
        item = runner.run(runtime.store.aget(rb._NAMESPACE, "reports/r.md"))
        assert item is not None
        assert isinstance(item.value, dict)
        assert item.value["content"] == "# Report"
        assert item.value["encoding"] == "utf-8"

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

        async def astream(self, stream_input, config=None):
            self.calls.append((stream_input, config))
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
        from langchain_core.messages import AIMessageChunk, ToolMessage

        monkeypatch.delenv("STREAMLIT_AGENT_PRINT", raising=False)
        agent = self._FakeAsyncAgent([
            {"messages": [AIMessageChunk(
                content="", tool_calls=[{"name": "nmap_scan", "args": {"target": "10.0.0.1"},
                                         "id": "c1", "type": "tool_call"}])]},
            {"messages": [ToolMessage(content="80/tcp open", tool_call_id="c1", name="nmap_scan")]},
            {"messages": [AIMessageChunk(content="Scan complete.")]},
        ])
        events = self._events(backend, agent, "scan target", "th1")
        assert [e["type"] for e in events] == ["tool_call", "tool_result", "message"]
        assert events[0]["data"] == {"name": "nmap_scan", "args": {"target": "10.0.0.1"}}
        assert events[1]["data"]["name"] == "nmap_scan"
        assert events[2]["data"] == "Scan complete."
        stream_input, config = agent.calls[0]
        assert stream_input == {"messages": [{"role": "user", "content": "scan target"}]}
        assert config["configurable"]["thread_id"] == "th1"
        assert config["configurable"]["checkpoint_ns"] == ""

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