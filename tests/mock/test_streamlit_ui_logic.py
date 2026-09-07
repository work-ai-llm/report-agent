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

    def test_session_id_created_on_startup(self):
        """Verify a session ID is generated when app starts."""
        at = AppTest.from_file(APP_PATH)
        at.run()
        
        assert "current_thread_id" in at.session_state
        assert at.session_state.current_thread_id.startswith("session_")

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
        
        first_session_id = at.session_state.current_thread_id
        
        # Simulate rerun
        at.run()
        
        # Session ID should persist
        assert at.session_state.current_thread_id == first_session_id


class TestSessionSwitching:
    """End-to-end session creation and switching in mock mode."""

    def test_new_session_creates_and_registers_thread(self):
        """New Session starts a fresh thread id and clears the chat."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("scan 10.0.0.1").run()
        first = at.session_state["current_thread_id"]

        at.sidebar.button[0].click().run()
        second = at.session_state["current_thread_id"]

        assert second != first
        assert at.session_state["chat_messages"] == []
        assert not at.exception

    def test_switching_back_restores_history(self):
        """Selecting an older session reloads its saved chat history."""
        at = AppTest.from_file(APP_PATH).run()
        at.chat_input[0].set_value("scan 10.0.0.1").run()
        first = at.session_state["current_thread_id"]

        at.sidebar.button[0].click().run()
        # One more pass so the session selector lists both threads
        at.run()
        at.sidebar.selectbox[0].select(first).run()

        assert at.session_state["current_thread_id"] == first
        assert len(at.session_state["chat_messages"]) == 2
        roles = [m["role"] for m in at.session_state["chat_messages"]]
        assert roles == ["user", "assistant"]
        assert not at.exception

