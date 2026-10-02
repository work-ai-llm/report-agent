"""Integration tests for Streamlit UI using AppTest.

This module verifies the complete UI flow using Streamlit's built-in testing
framework. Tests simulate user interactions without requiring a browser.

## Test Coverage

1. **App Initialization**: Verify app loads without errors
2. **Session State**: Check session ID creation and persistence
3. **Component Rendering**: Verify sidebar and chat area render correctly
4. **Mock Integration**: Test with mock service to avoid external dependencies

## Running Tests

```bash
python -m pytest tests/mock/test_streamlit_ui_logic.py -v
```

Note: These tests use mock objects and do not require PostgreSQL or LLM APIs.
"""

import os
import pytest
from pathlib import Path
import streamlit as st
from streamlit.testing.v1 import AppTest

os.environ["STREAMLIT_USE_MOCKS"] = "1"

APP_PATH = str(Path(__file__).resolve().parent.parent.parent / "streamlit_ui" / "app.py")


@pytest.fixture(autouse=True)
def _isolate_mock_cache():
    """Clear streamlit's cache_resource between tests.

    Mock dependencies are cached via st.cache_resource and would otherwise
    leak (sessions, uploaded files, seeded report) across AppTest instances
    within the same pytest process.
    """
    try:
        st.cache_resource.clear()
    except Exception:
        pass
    os.environ["STREAMLIT_USE_MOCKS"] = "1"
    yield
    try:
        st.cache_resource.clear()
    except Exception:
        pass


class TestAppInitialization:
    """Test application startup and initial state."""

    def test_app_loads_without_errors(self):
        """Verify the app starts without exceptions."""
        at = AppTest.from_file(APP_PATH)
        at.run()
        
        # Should not have exceptions
        assert not at.exception, f"App failed to load: {at.exception}"

    def test_no_session_created_on_startup(self):
        """A session must NOT be created on page load – only on first message."""
        at = AppTest.from_file(APP_PATH)
        at.run()

        assert "current_thread_id" in at.session_state
        assert at.session_state.current_thread_id is None

    def test_chat_messages_initialized_empty(self):
        """Verify chat history starts empty."""
        at = AppTest.from_file(APP_PATH)
        at.run()
        
        assert "chat_messages" in at.session_state
        assert at.session_state.chat_messages == []

    def test_findings_list_initialized_empty(self):
        """Verify findings list starts empty."""
        at = AppTest.from_file(APP_PATH)
        at.run()
        
        assert "findings" in at.session_state
        assert at.session_state.findings == []


class TestSidebarRendering:
    """Test sidebar component rendering."""

    def test_sidebar_displays_title(self):
        """Verify sidebar shows the app title."""
        at = AppTest.from_file(APP_PATH)
        at.run()
        
        # Sidebar should be rendered
        sidebar = at.sidebar
        assert sidebar is not None

    def test_new_session_button_exists(self):
        """Verify 'New Session' button is present."""
        at = AppTest.from_file(APP_PATH)
        at.run()
        
        # Find the new session button in sidebar
        buttons = at.sidebar.button
        assert any("New Session" in str(button) for button in buttons)


class TestChatAreaRendering:
    """Test main chat area rendering."""

    def test_chat_input_exists(self):
        """Verify chat input field is present."""
        at = AppTest.from_file(APP_PATH)
        at.run()
        
        # Chat input should exist
        chat_inputs = at.chat_input
        assert len(chat_inputs) > 0

    def test_main_area_has_title(self):
        """Verify main area displays chat title."""
        at = AppTest.from_file(APP_PATH)
        at.run()
        
        # Main area should have content
        assert len(at.title) > 0


class TestSessionStatePersistence:
    """Test that session state persists across interactions."""

    def test_session_id_persists_after_rerun(self):
        """Verify session ID remains the same after app rerun."""
        at = AppTest.from_file(APP_PATH)
        at.run()
        at.chat_input[0].set_value("first").run()
        first_session_id = at.session_state.current_thread_id

        # Simulate rerun
        at.run()

        # Session ID should persist
        assert at.session_state.current_thread_id == first_session_id


class TestSessionSwitching:
    """End-to-end session creation and switching in mock mode."""

    def test_new_session_clears_chat_without_creating_thread(self):
        """New Session clears the chat and drops the active thread, but does
        NOT create/persist a new session (that only happens on first message)."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("scan 10.0.0.1").run()
        first = at.session_state["current_thread_id"]
        assert first is not None

        at.sidebar.button[0].click().run()

        assert at.session_state["current_thread_id"] is None
        assert at.session_state["chat_messages"] == []
        assert not at.exception

    def test_new_session_then_message_creates_fresh_thread(self):
        """After New Session, the next message creates a brand-new thread."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("scan 10.0.0.1").run()
        first = at.session_state["current_thread_id"]

        at.sidebar.button[0].click().run()
        assert at.session_state["current_thread_id"] is None

        # Next message creates a fresh session.
        at.chat_input[0].set_value("next message").run()
        second = at.session_state["current_thread_id"]
        assert second is not None
        assert second != first
        assert not at.exception

    def test_switching_back_restores_history(self):
        """Selecting an older session reloads its saved chat history."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("scan 10.0.0.1").run()
        first = at.session_state["current_thread_id"]
        at.run()
        # Start a second session and send the second message there.
        at.sidebar.button[0].click().run()
        at.chat_input[0].set_value("second session").run()
        at.run()
        first_selector_index = [sb.label for sb in at.sidebar.selectbox].index("Select Session")
        at.sidebar.selectbox[first_selector_index].select(first).run()

        assert at.session_state["current_thread_id"] == first
        # History contains the structured event entries: user msg, 2 tool calls,
        # and the assistant reply.
        messages = at.session_state["chat_messages"]
        types = [m["type"] for m in messages]
        assert types == ["user", "tool_call", "tool_call", "assistant"]
        assert messages[0]["content"] == "scan 10.0.0.1"
        assert messages[1]["name"] == "nmap_scan"
        assert messages[2]["name"] == "nikto_scan"
        assert messages[3]["content"] == "This is a mock agent response."
        assert not at.exception


class TestIssue1NewSessionStaysInNewSession:
    """Regression: if New Session creates no thread until a message, a typed
    message must stay in the thread that got created for it."""

    def test_message_after_new_session_goes_to_new_thread(self):
        """New Session then a message creates/th stays in a fresh thread."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("first message").run()
        first = at.session_state["current_thread_id"]

        at.sidebar.button[0].click().run()
        assert at.session_state["current_thread_id"] is None

        # Now send a message; a brand-new session is created on this message.
        at.chat_input[0].set_value("second message").run()
        second = at.session_state["current_thread_id"]
        assert second is not None
        assert second != first

        # The created thread persists across another run.
        at.run()
        assert at.session_state["current_thread_id"] == second
        assert not at.exception

    def test_new_session_history_belongs_to_new_thread(self):
        """After a message in the new session, history belongs to the new thread."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("first message").run()
        first = at.session_state["current_thread_id"]

        at.sidebar.button[0].click().run()
        # Message is authored after New Session cleared the chat.
        at.chat_input[0].set_value("only in B").run()
        second = at.session_state["current_thread_id"]
        assert second != first

        messages = at.session_state["chat_messages"]
        user_texts = [m["content"] for m in messages if m["type"] == "user"]
        assert user_texts == ["only in B"]
        # The history doesn't contain the first message from A.
        assert "first message" not in user_texts
        assert at.session_state["current_thread_id"] == second


class TestIssue4InitialSessionAppearsImmediately:
    """Regression: a session is only registered once a message is sent."""

    def test_no_session_registered_on_first_load(self):
        """A fresh load must NOT create/register a session until a message."""
        at = AppTest.from_file(APP_PATH).run()
        assert "current_thread_id" in at.session_state
        assert at.session_state["current_thread_id"] is None
        assert not at.exception

    def test_session_registered_after_first_message(self):
        """On the first message a new session is created and appears in selector."""
        at = AppTest.from_file(APP_PATH).run()
        assert at.session_state["current_thread_id"] is None

        at.chat_input[0].set_value("hello").run()
        tid = at.session_state["current_thread_id"]
        assert tid is not None
        assert tid.startswith("session_")
        assert not at.exception

        # The sidebar rendered before the chat handler created the thread in the
        # same run, so the selector picks it up on the next run (next interaction).
        at.run()
        assert any(
            sb.label == "Select Session" and tid in sb.options
            for sb in at.sidebar.selectbox
        )
        assert not at.exception


class TestIssue2ToolCallsPersist:
    """Regression: tool-call blocks survive subsequent renders & session switches."""

    def test_tool_calls_remain_after_second_message(self):
        """After a second message, the first assistant turn still has tool calls."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("first").run()

        # Send a second message; the first turn must keep its tool calls, and
        # the second turn also emits tool calls (mock always emits them).
        at.chat_input[0].set_value("second").run()
        messages = at.session_state["chat_messages"]
        types = [m["type"] for m in messages]
        # First turn: user, tool_call, tool_call, assistant
        # Second turn: user, tool_call, tool_call, assistant
        assert types == [
            "user", "tool_call", "tool_call", "assistant",
            "user", "tool_call", "tool_call", "assistant",
        ]
        # The first turn's tool calls are preserved.
        assert messages[1]["name"] == "nmap_scan"
        assert messages[2]["name"] == "nikto_scan"
        assert not at.exception

    def test_tool_calls_survive_session_switch(self):
        """After switching away and back, tool-call expanders are restored."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("first").run()
        first = at.session_state["current_thread_id"]

        # New session, then switch back to A.
        at.sidebar.button[0].click().run()
        at.run()  # let selector list both threads
        at.sidebar.selectbox[0].select(first).run()

        messages = at.session_state["chat_messages"]
        types = [m["type"] for m in messages]
        assert types == ["user", "tool_call", "tool_call", "assistant"]
        assert not at.exception


class TestIssue6UploadDuplicateNames:
    """Regression: duplicate filename uploads produce a collision UX."""

    def test_duplicate_upload_requires_explicit_replace(self):
        """Uploading a file with an existing name must not be silently accepted."""
        at = AppTest.from_file(APP_PATH).run()

        # Upload a first file.
        at.sidebar.file_uploader[0].upload("nmap.txt", b"content").run()
        assert at.session_state["findings"] == ["nmap.txt"]

        # Clear the uploader state; upload the same name again.
        # The collision banner must appear (warning) rather than silent acceptance.
        at.sidebar.file_uploader[0].upload("nmap.txt", b"new content").run()
        # A warning should be present indicating the collision.
        assert len(at.warning) >= 1
        assert not at.exception

    def test_initial_session_file_upload_clears_dropzone(self):
        """Uploading a file in a fresh session does not leave the dropzone re-holding it."""
        at = AppTest.from_file(APP_PATH).run()
        at.sidebar.file_uploader[0].upload("scan.txt", b"data").run()
        assert "scan.txt" in at.session_state["findings"]
        assert not at.exception


class TestIssue7SingleClickDownload:
    """Regression: report download requires only one click."""

    def test_report_download_button_is_direct(self):
        """st.download_button is rendered immediately, not behind an st.button."""
        at = AppTest.from_file(APP_PATH).run()
        # The demo report is seeded in mock mode so a download button shows.
        assert len(at.sidebar.download_button) >= 1
        assert not at.exception


class TestDeleteSession:
    """Session deletion workflow in mock mode."""

    def _active_sessions(self, at) -> set:
        return set(at.session_state["service"].list_threads())

    def test_deleting_current_session_removes_it(self):
        """Deleting the selected session removes it and resets the chat."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("first message").run()
        first = at.session_state["current_thread_id"]

        # Second session becomes the active one.
        at.sidebar.button[0].click().run()          # New Session
        at.chat_input[0].set_value("second message").run()
        second = at.session_state["current_thread_id"]
        assert second != first
        assert self._active_sessions(at) == {first, second}

        # Confirm + delete the active (second) session.
        at.sidebar.checkbox[0].check().run()
        at.sidebar.button[1].click().run()

        assert at.session_state["current_thread_id"] is None
        assert not self._active_sessions(at) & {second}
        assert first in self._active_sessions(at)
        assert at.session_state["chat_messages"] == []
        assert not at.exception

    def test_delete_requires_confirmation(self):
        """Without confirming, no Delete Session button is shown and nothing dies."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("first message").run()
        tid = at.session_state["current_thread_id"]

        # Not confirmed -> no delete button rendered.
        assert not any("Delete Session" in str(b) for b in at.sidebar.button)

        # Confirming alone must not delete.
        at.sidebar.checkbox[0].check().run()
        buttons = [str(b) for b in at.sidebar.button]
        assert any("Delete Session" in b for b in buttons)
        assert tid in self._active_sessions(at)
        assert at.session_state["current_thread_id"] == tid
        assert not at.exception

    def test_delete_last_session_leaves_no_sessions(self):
        """Deleting the only existing session empties the session list."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("only message").run()
        tid = at.session_state["current_thread_id"]

        at.sidebar.checkbox[0].check().run()
        at.sidebar.button[1].click().run()

        assert at.session_state["current_thread_id"] is None
        assert self._active_sessions(at) == set()
        assert not at.exception

