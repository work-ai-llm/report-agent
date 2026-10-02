"""Tests for PenTestAgentService using mock objects.

This module verifies the service layer logic without requiring PostgreSQL,
LLM APIs, or any external dependencies. All tests use the mock implementations
from PR #1.

## Test Coverage

1. **Service Initialization**: Verify dependency injection works correctly
2. **File Upload/Download**: Test findings upload and report download flows
3. **Global Namespace**: Confirm findings are shared across service instances
4. **Agent Streaming**: Verify tool call events are yielded properly
5. **Session Persistence**: Test chat history save/retrieve operations

## Running Tests

```bash
python -m pytest tests/mock/test_agent_service_with_mocks.py -v
```

All tests should pass in isolation without external services.

## What These Tests Do and Don't Prove

The mocks (PR #1) are permissive in-memory dictionaries - they accept any content
and never reject or transform input (see their Permissiveness Caveat). As a result:

- These tests DO verify the service layer's orchestration logic: path prefix
  conventions (findings/, reports/), thread isolation via thread_id, None/default
  handling, and correct forwarding of message/thread_id into the injected agent.
- These tests DO NOT prove compatibility with real checkpointer/file store/agent
  objects. Real LangGraph/Postgres objects expose different method signatures than
  the protocols (e.g. aget_tuple/aput vs get/put), so they require adapter classes,
  and those adapters must be validated by an integration test against a real
  backend (SQLite fallback is enough) before production use.
"""

import pytest
from streamlit_ui.services.agent_service import PenTestAgentService
from tests.mock.mock_objects_streamlit import (
    MockCheckpointer,
    MockFileStore,
    MockAgentRunner,
)


class TestFindingsUpload:
    """Test findings file upload functionality."""

    def test_upload_finding_stores_in_global_fs(self):
        """Verify uploaded findings are stored under 'findings/' prefix."""
        mock_file_store = MockFileStore()
        service = PenTestAgentService(
            thread_id="session_1",
            checkpointer=MockCheckpointer(),
            file_store=mock_file_store,
            agent_runner=MockAgentRunner(),
        )

        service.upload_finding("nmap_scan.txt", b"Port 80 open\nPort 22 open")

        # Verify file exists with correct path
        content = mock_file_store.read_file("findings/nmap_scan.txt")
        assert content == b"Port 80 open\nPort 22 open"

    def test_list_findings_returns_filenames_without_prefix(self):
        """Verify list_findings strips the 'findings/' prefix."""
        mock_file_store = MockFileStore()
        mock_file_store.write_file("findings/report1.txt", b"content1")
        mock_file_store.write_file("findings/report2.txt", b"content2")

        service = PenTestAgentService(
            thread_id="session_1",
            checkpointer=MockCheckpointer(),
            file_store=mock_file_store,
            agent_runner=MockAgentRunner(),
        )

        findings = service.list_findings()
        assert "report1.txt" in findings
        assert "report2.txt" in findings
        assert "findings/report1.txt" not in findings  # Prefix stripped

    def test_delete_finding_removes_from_fs(self):
        """Verify delete_finding removes file from storage."""
        mock_file_store = MockFileStore()
        mock_file_store.write_file("findings/to_delete.txt", b"content")

        service = PenTestAgentService(
            thread_id="session_1",
            checkpointer=MockCheckpointer(),
            file_store=mock_file_store,
            agent_runner=MockAgentRunner(),
        )

        result = service.delete_finding("to_delete.txt")
        assert result is True
        assert mock_file_store.read_file("findings/to_delete.txt") is None

    def test_delete_nonexistent_finding_returns_false(self):
        """Verify deleting non-existent file returns False."""
        service = PenTestAgentService(
            thread_id="session_1",
            checkpointer=MockCheckpointer(),
            file_store=MockFileStore(),
            agent_runner=MockAgentRunner(),
        )

        result = service.delete_finding("nonexistent.txt")
        assert result is False


class TestGlobalNamespace:
    """Test that findings are shared across service instances (global namespace)."""

    def test_findings_shared_between_sessions(self):
        """Verify findings uploaded by one session are visible to another."""
        shared_file_store = MockFileStore()

        # Session 1 uploads finding
        service1 = PenTestAgentService(
            thread_id="session_1",
            checkpointer=MockCheckpointer(),
            file_store=shared_file_store,
            agent_runner=MockAgentRunner(),
        )
        service1.upload_finding("shared_finding.txt", b"Shared content")

        # Session 2 should see the same finding
        service2 = PenTestAgentService(
            thread_id="session_2",
            checkpointer=MockCheckpointer(),
            file_store=shared_file_store,
            agent_runner=MockAgentRunner(),
        )
        findings = service2.list_findings()

        assert "shared_finding.txt" in findings
        content = shared_file_store.read_file("findings/shared_finding.txt")
        assert content == b"Shared content"


class TestAgentStreaming:
    """Test agent message streaming with tool calls."""

    def test_send_message_yields_tool_calls(self):
        """Verify tool calls are streamed as events."""
        mock_agent = MockAgentRunner()
        mock_agent.set_tool_calls([
            {"name": "nmap_scan", "args": {"target": "10.0.0.1"}},
            {"name": "nikto_scan", "args": {"url": "http://10.0.0.1"}},
        ])

        service = PenTestAgentService(
            thread_id="session_1",
            checkpointer=MockCheckpointer(),
            file_store=MockFileStore(),
            agent_runner=mock_agent,
        )

        events = list(service.send_message("Scan target"))

        # Should have 2 tool calls + 1 final message
        tool_call_events = [e for e in events if e["type"] == "tool_call"]
        assert len(tool_call_events) == 2
        assert tool_call_events[0]["data"]["name"] == "nmap_scan"
        assert tool_call_events[1]["data"]["name"] == "nikto_scan"

        # The mock ignores its args, so only these asserts catch a service bug
        # that forwards the wrong message/thread_id to the agent runner.
        assert mock_agent.last_thread_id == "session_1"
        assert mock_agent.last_message == "Scan target"

    def test_send_message_yields_final_message(self):
        """Verify final agent response is yielded after tool calls."""
        mock_agent = MockAgentRunner()
        mock_agent.set_final_message("Scan complete. Found 2 open ports.")

        service = PenTestAgentService(
            thread_id="session_1",
            checkpointer=MockCheckpointer(),
            file_store=MockFileStore(),
            agent_runner=mock_agent,
        )

        events = list(service.send_message("Scan target"))

        message_events = [e for e in events if e["type"] == "message"]
        assert len(message_events) == 1
        assert message_events[0]["data"] == "Scan complete. Found 2 open ports."

        # Same guard as above: verify correct forwarding of inputs.
        assert mock_agent.last_thread_id == "session_1"
        assert mock_agent.last_message == "Scan target"


class TestReportDownload:
    """Test report download functionality."""

    def test_download_report_retrieves_content(self):
        """Verify reports can be downloaded from virtual filesystem."""
        mock_file_store = MockFileStore()
        mock_file_store.prepopulate_with_report("pentest_report.md", b"# Report\n\n## Findings")

        service = PenTestAgentService(
            thread_id="session_1",
            checkpointer=MockCheckpointer(),
            file_store=mock_file_store,
            agent_runner=MockAgentRunner(),
        )

        content = service.download_report("pentest_report.md")
        assert content == b"# Report\n\n## Findings"

    def test_download_nonexistent_report_returns_none(self):
        """Verify downloading non-existent report returns None."""
        service = PenTestAgentService(
            thread_id="session_1",
            checkpointer=MockCheckpointer(),
            file_store=MockFileStore(),
            agent_runner=MockAgentRunner(),
        )

        content = service.download_report("nonexistent.md")
        assert content is None

    def test_list_reports_returns_filenames_without_prefix(self):
        """Verify list_reports strips the 'reports/' prefix."""
        mock_file_store = MockFileStore()
        mock_file_store.prepopulate_with_report("report1.md", b"content1")
        mock_file_store.prepopulate_with_report("report2.md", b"content2")

        service = PenTestAgentService(
            thread_id="session_1",
            checkpointer=MockCheckpointer(),
            file_store=mock_file_store,
            agent_runner=MockAgentRunner(),
        )

        reports = service.list_reports()
        assert "report1.md" in reports
        assert "report2.md" in reports
        assert "reports/report1.md" not in reports


class TestSessionDeletion:
    """Test session/thread deletion through the service layer."""

    def test_delete_thread_removes_from_checkpointer(self):
        """Verify delete_thread removes the thread and its history."""
        mock_checkpointer = MockCheckpointer()
        mock_checkpointer.put("session_1", {"messages": [{"role": "user", "content": "hi"}]})
        mock_checkpointer.put("session_2", {"messages": [{"role": "user", "content": "bye"}]})

        service = PenTestAgentService(
            thread_id="session_1",
            checkpointer=mock_checkpointer,
            file_store=MockFileStore(),
            agent_runner=MockAgentRunner(),
        )

        result = service.delete_thread("session_1")

        assert result is True
        assert "session_1" not in mock_checkpointer.list_threads()
        assert service.get_chat_history() == []
        # Unrelated threads are untouched.
        assert "session_2" in mock_checkpointer.list_threads()

    def test_delete_nonexistent_thread_returns_false(self):
        """Verify deleting a thread that does not exist returns False."""
        service = PenTestAgentService(
            thread_id="session_1",
            checkpointer=MockCheckpointer(),
            file_store=MockFileStore(),
            agent_runner=MockAgentRunner(),
        )

        result = service.delete_thread("does_not_exist")

        assert result is False


class TestChatHistoryPersistence:
    """Test chat history save and retrieve operations."""

    def test_get_chat_history_empty_initially(self):
        """Verify new session starts with empty history."""
        service = PenTestAgentService(
            thread_id="new_session",
            checkpointer=MockCheckpointer(),
            file_store=MockFileStore(),
            agent_runner=MockAgentRunner(),
        )

        history = service.get_chat_history()
        assert history == []

    def test_save_and_retrieve_chat_history(self):
        """Verify chat history persists correctly."""
        mock_checkpointer = MockCheckpointer()
        messages = [
            {"role": "user", "content": "Scan target"},
            {"role": "assistant", "content": "Scanning..."},
        ]

        service = PenTestAgentService(
            thread_id="session_1",
            checkpointer=mock_checkpointer,
            file_store=MockFileStore(),
            agent_runner=MockAgentRunner(),
        )

        service.save_chat_history(messages)
        retrieved = service.get_chat_history()

        assert retrieved == messages

    def test_chat_history_isolated_per_thread(self):
        """Verify different threads have separate histories."""
        mock_checkpointer = MockCheckpointer()

        service1 = PenTestAgentService(
            thread_id="thread_1",
            checkpointer=mock_checkpointer,
            file_store=MockFileStore(),
            agent_runner=MockAgentRunner(),
        )
        service1.save_chat_history([{"role": "user", "content": "Thread 1 message"}])

        service2 = PenTestAgentService(
            thread_id="thread_2",
            checkpointer=mock_checkpointer,
            file_store=MockFileStore(),
            agent_runner=MockAgentRunner(),
        )
        service2.save_chat_history([{"role": "user", "content": "Thread 2 message"}])

        # Verify isolation
        assert len(service1.get_chat_history()) == 1
        assert len(service2.get_chat_history()) == 1
        assert service1.get_chat_history()[0]["content"] == "Thread 1 message"
        assert service2.get_chat_history()[0]["content"] == "Thread 2 message"
