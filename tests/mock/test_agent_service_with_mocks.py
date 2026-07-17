"""
Tests for PenTestAgentService using mock objects.

These tests verify the service layer logic without requiring:
- PostgreSQL database
- LLM API access
- Real agent initialization

All tests use injected mock objects from tests.mock.mock_objects_streamlit.
"""

import pytest
import asyncio
import sys
from pathlib import Path

# Add workspace to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from tests.mock.mock_objects_streamlit import (
    MockStore,
    MockCheckpointer, 
    MockAgent,
    create_mock_service_components
)
from streamlit_ui.services.agent_service import (
    PenTestAgentService,
    ToolCallEvent,
    AgentMessageEvent
)


class TestPenTestAgentServiceInitialization:
    """Test service initialization in mock and production modes."""
    
    def test_mock_mode_detection(self):
        """Verify service correctly identifies mock mode when objects injected."""
        store = MockStore()
        checkpointer = MockCheckpointer()
        agent = MockAgent()
        
        service = PenTestAgentService(
            store=store,
            checkpointer=checkpointer,
            agent=agent
        )
        
        assert service.is_mock_mode is True
        
    def test_production_mode_detection(self):
        """Verify service correctly identifies production mode when no objects injected."""
        service = PenTestAgentService()
        
        assert service.is_mock_mode is False
        
    def test_thread_id_initialization(self):
        """Verify thread_id is set correctly on initialization."""
        service = PenTestAgentService(thread_id="session_123")
        
        assert service.current_thread_id == "session_123"
        
    def test_thread_id_switching(self):
        """Verify thread_id can be switched for session selection."""
        service = PenTestAgentService(thread_id="session_A")
        
        service.set_thread_id("session_B")
        
        assert service.current_thread_id == "session_B"


class TestFindingsUploadWithMocks:
    """Test findings upload functionality with mock store."""
    
    @pytest.mark.asyncio
    async def test_upload_finding_success(self):
        """Verify finding file can be uploaded to mock store."""
        store = MockStore()
        service = PenTestAgentService(store=store, thread_id="test")
        
        result = await service.upload_finding("nmap_scan.txt", "Port 80 open")
        
        assert result is True
        
        # Verify content was stored
        item = await store.aget(("filesystem",), "findings/nmap_scan.txt")
        assert item is not None
        assert item.value["content"] == "Port 80 open"
        
    @pytest.mark.asyncio
    async def test_upload_finding_no_store(self):
        """Verify upload fails gracefully when no store available."""
        service = PenTestAgentService()
        
        result = await service.upload_finding("test.txt", "content")
        
        assert result is False
        
    @pytest.mark.asyncio
    async def test_list_findings_displays_files(self):
        """Verify list_findings shows uploaded files in UI-readable format."""
        store, _, _ = create_mock_service_components(
            preloaded_findings={
                "nmap_results.txt": "Ports: 22, 80, 443",
                "bloodhound_dump.json": '{"nodes": []}',
                "web_scans.log": "GET /admin 200 OK"
            }
        )
        service = PenTestAgentService(store=store, thread_id="test")
        
        findings = await service.list_findings()
        
        assert len(findings) == 3
        assert "nmap_results.txt" in findings
        assert "bloodhound_dump.json" in findings
        assert "web_scans.log" in findings


class TestGlobalFindingsNamespace:
    """Test that findings are shared across sessions (global namespace)."""
    
    @pytest.mark.asyncio
    async def test_findings_shared_across_sessions(self):
        """Verify two different service instances access same findings."""
        # Create single shared store
        store = MockStore()
        
        # Upload finding from session A
        service_a = PenTestAgentService(store=store, thread_id="session_A")
        await service_a.upload_finding("shared_finding.txt", "Uploaded by A")
        
        # Access from session B
        service_b = PenTestAgentService(store=store, thread_id="session_B")
        findings = await service_b.list_findings()
        
        # Session B should see session A's upload
        assert "shared_finding.txt" in findings
        
        # Verify content is identical
        item = await store.aget(("filesystem",), "findings/shared_finding.txt")
        assert item.value["content"] == "Uploaded by A"
        
    @pytest.mark.asyncio
    async def test_multiple_uploads_accumulate(self):
        """Verify multiple uploads from different sessions accumulate."""
        store = MockStore()
        
        service_a = PenTestAgentService(store=store, thread_id="session_A")
        service_b = PenTestAgentService(store=store, thread_id="session_B")
        
        await service_a.upload_finding("from_a.txt", "A's content")
        await service_b.upload_finding("from_b.txt", "B's content")
        
        # Both sessions see all findings
        findings_a = await service_a.list_findings()
        findings_b = await service_b.list_findings()
        
        assert len(findings_a) == 2
        assert len(findings_b) == 2
        assert "from_a.txt" in findings_a
        assert "from_b.txt" in findings_b


class TestReportDownloadWithMocks:
    """Test report download functionality with mock store."""
    
    @pytest.mark.asyncio
    async def test_download_report_success(self):
        """Verify report can be downloaded from mock store."""
        store = MockStore()
        service = PenTestAgentService(store=store, thread_id="session_123")
        
        # Pre-load a report
        await store.aput(
            ("filesystem",),
            "reports/session_123/final_report.md",
            {"content": "# Pentest Report\n## Findings\nCritical vulnerability found."}
        )
        
        content = await service.download_report("final_report.md")
        
        assert content is not None
        assert "# Pentest Report" in content
        assert "Critical vulnerability" in content
        
    @pytest.mark.asyncio
    async def test_download_report_not_found(self):
        """Verify download returns None for non-existent report."""
        store = MockStore()
        service = PenTestAgentService(store=store, thread_id="session_456")
        
        content = await service.download_report("missing_report.md")
        
        assert content is None
        
    @pytest.mark.asyncio
    async def test_download_button_logic(self):
        """
        Verify report download button logic works correctly.
        
        This tests the core requirement: "if report button is pressed, 
        would it be downloaded as intended by the logic?"
        """
        store = MockStore()
        service = PenTestAgentService(store=store, thread_id="download_test")
        
        # Simulate report existing in store (as if agent created it)
        report_content = """# Security Assessment Report
Executive Summary
==================
Multiple critical findings discovered.

Recommendations
===============
1. Patch immediately
2. Rotate credentials
"""
        await store.aput(
            ("filesystem",),
            "reports/download_test/security_report.md",
            {"content": report_content}
        )
        
        # Simulate user clicking download button
        downloaded = await service.download_report("security_report.md")
        
        # Verify download returns exact content
        assert downloaded == report_content
        assert downloaded.startswith("# Security Assessment Report")
        
    @pytest.mark.asyncio
    async def test_reports_are_session_isolated(self):
        """Verify reports are stored per-session, not global."""
        store = MockStore()
        
        # Upload report to session A
        service_a = PenTestAgentService(store=store, thread_id="session_A")
        await store.aput(
            ("filesystem",),
            "reports/session_A/private_report.md",
            {"content": "Session A only"}
        )
        
        # Try to access from session B
        service_b = PenTestAgentService(store=store, thread_id="session_B")
        
        # Session B cannot access session A's reports
        content = await service_b.download_report("private_report.md")
        assert content is None
        
        # But session A can still access its own
        content_a = await service_a.download_report("private_report.md")
        assert content_a == "Session A only"


class TestAgentToolCallsVisible:
    """Test that agent tool calls are streamed and visible in UI."""
    
    @pytest.mark.asyncio
    async def test_tool_call_events_streamed(self):
        """Verify mock agent yields tool call events that UI can render."""
        agent = MockAgent()
        agent.configure_response(
            tool_calls=[
                {
                    'name': 'ls',
                    'args': {'path': '/findings/'}
                },
                {
                    'name': 'read_file',
                    'args': {'file_path': '/findings/nmap.txt'}
                }
            ],
            final_message="Analysis complete.",
            tool_results=["nmap.txt\nbloodhound.json", "Port 80 open"]
        )
        
        service = PenTestAgentService(agent=agent, thread_id="test")
        
        # Collect all streamed events
        events = []
        async for event_type, event_data in service.execute_agent("Analyze findings"):
            events.append((event_type, event_data))
            
        # Verify tool calls were streamed
        tool_call_events = [e for e in events if e[0] == 'tool_call']
        assert len(tool_call_events) == 2
        
        # Verify first tool call details
        first_tc = tool_call_events[0][1]
        assert isinstance(first_tc, ToolCallEvent)
        assert first_tc.tool_name == 'ls'
        assert first_tc.args['path'] == '/findings/'
        
        # Verify final message was streamed
        message_events = [e for e in events if e[0] == 'message']
        assert len(message_events) > 0
        
    @pytest.mark.asyncio
    async def test_tool_call_preview_for_large_content(self):
        """Verify large file content is previewed in tool calls."""
        agent = MockAgent()
        agent.configure_response(
            tool_calls=[
                {
                    'name': 'write_file',
                    'args': {
                        'file_path': '/reports/draft.md',
                        'content': 'A' * 500  # Large content
                    }
                }
            ],
            final_message="Report draft saved."
        )
        
        service = PenTestAgentService(agent=agent, thread_id="test")
        
        events = []
        async for event_type, event_data in service.execute_agent("Save draft"):
            if event_type == 'tool_call':
                events.append(event_data)
                
        assert len(events) == 1
        tc_event = events[0]
        
        # Verify preview was created (truncated content)
        assert tc_event.preview_args is not None
        assert len(tc_event.preview_args['content']) < 150
        assert tc_event.preview_args['content'].endswith("...")
        
        # Original args unchanged
        assert len(tc_event.args['content']) == 500


class TestSessionHistoryRetrieval:
    """Test session history retrieval for conversation persistence."""
    
    @pytest.mark.asyncio
    async def test_get_session_history(self):
        """Verify conversation history can be retrieved for current session."""
        checkpointer = MockCheckpointer()
        service = PenTestAgentService(checkpointer=checkpointer, thread_id="history_test")
        
        # Pre-populate checkpoint with messages
        await checkpointer.aput(
            {"configurable": {"thread_id": "history_test"}},
            [
                {"role": "user", "content": "Start pentest"},
                {"role": "assistant", "content": "Running nmap scan..."},
                {"role": "user", "content": "What did you find?"},
                {"role": "assistant", "content": "Found open ports 22, 80, 443"}
            ]
        )
        
        history = await service.get_session_history()
        
        assert len(history) == 4
        assert history[0]["role"] == "user"
        assert history[-1]["content"] == "Found open ports 22, 80, 443"


class TestMockObjectIsolation:
    """Test that mock objects provide proper test isolation."""
    
    @pytest.mark.asyncio
    async def test_mock_store_clear(self):
        """Verify mock store can be cleared between tests."""
        store = MockStore()
        await store.aput(("filesystem",), "test.txt", {"content": "data"})
        
        items_before = [item async for item in store.asearch(("filesystem",))]
        assert len(items_before) == 1
        
        await store.clear()
        
        items_after = [item async for item in store.asearch(("filesystem",))]
        assert len(items_after) == 0
        
    @pytest.mark.asyncio
    async def test_mock_checkpointer_clear(self):
        """Verify mock checkpointer can be cleared between tests."""
        checkpointer = MockCheckpointer()
        await checkpointer.aput(
            {"configurable": {"thread_id": "test"}},
            [{"role": "user", "content": "test"}]
        )
        
        checkpoint = await checkpointer.aget({"configurable": {"thread_id": "test"}})
        assert checkpoint is not None
        
        await checkpointer.clear()
        
        checkpoint_after = await checkpointer.aget({"configurable": {"thread_id": "test"}})
        assert checkpoint_after is None
        
    @pytest.mark.asyncio
    async def test_mock_agent_history_clear(self):
        """Verify mock agent call history can be cleared."""
        agent = MockAgent()
        agent.configure_response(final_message="Response 1")
        
        # Make a call
        async for _ in agent.astream({"messages": [{"content": "Query 1"}]}):
            pass
            
        assert len(agent.get_call_history()) == 1
        
        agent.clear_history()
        
        assert len(agent.get_call_history()) == 0
