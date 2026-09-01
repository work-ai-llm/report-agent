"""Streamlit UI Application for Penetration Test Reporting Agent.

This is the main entry point for the Streamlit web interface that allows users
to interact with the pen-test reporting agent through a chat-based UI.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│                     Streamlit Browser UI                     │
├─────────────────────────────────────────────────────────────┤
│  Sidebar                        │  Main Chat Area           │
│  ─────────                      │  ───────────────          │
│  [Session Selector]             │                           │
│  [New Session]                  │  [User Messages]          │
│                                 │  [Agent Responses]        │
│  ───── Upload ─────             │  [Tool Call Visualizations]
│  [📁 File Uploader]             │                           │
│  • finding1.txt                 │  [Chat Input + Send]      │
│  • finding2.txt                 │                           │
│                                 │                           │
│  ───── Reports ─────            │                           │
│  [📄 Report Selector]           │                           │
│  [Download Button]              │                           │
└─────────────────────────────────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│              PenTestAgentService (Service Layer)             │
│  - upload_finding()                                          │
│  - send_message() → yields tool calls                        │
│  - download_report()                                         │
│  - list_findings() / list_reports()                          │
└────────┬──────────────────────┬──────────────────────────────┘
         │                      │
         ▼                      ▼
┌─────────────────┐    ┌──────────────────┐
│ Checkpointer    │    │ FileStore        │
│ (PostgresSaver) │    │ (PostgresStore)  │
└─────────────────┘    └──────────────────┘
         │
         ▼
┌─────────────────┐
│ AgentRunner     │
│ (LangGraph)     │
└─────────────────┘
```

## Data Flow Examples

### 1. User Sends Message
```
User types "Scan target" 
  → st.session_state.chat_messages appended
  → service.send_message("Scan target")
  → Yields tool_call events
  → render_tool_call() displays each tool
  → Yields final message
  → render_message() displays response
  → service.save_chat_history() persists to DB
```

### 2. User Uploads Finding
```
User selects file
  → render_file_uploader() receives bytes
  → Callback: service.upload_finding(filename, content)
  → file_store.write_file("findings/filename", content)
  → st.session_state.findings updated
  → Sidebar refreshes showing new file
```

### 3. User Downloads Report
```
User clicks download button
  → render_report_downloader() called
  → service.download_report("report.md")
  → file_store.read_file("reports/report.md")
  → st.download_button() triggers browser download
```

## Session State Management

The app uses `st.session_state` to maintain UI state across reruns:
- `current_thread_id`: Active session identifier
- `chat_messages`: List of conversation messages
- `findings`: List of uploaded finding filenames
- `service`: PenTestAgentService instance (cached)

## Running the Application

```bash
streamlit run streamlit_ui/app.py
```

## Environment Variables (Production)

- `DATABASE_URL`: PostgreSQL connection string for session store
- `AGENT_MODEL`: LLM model name for agent execution
"""

import os
import streamlit as st
from typing import Optional, Callable
from streamlit_ui.services.agent_service import PenTestAgentService
from streamlit_ui.components.chat_display import render_message, render_tool_call
from streamlit_ui.components.file_uploader import render_file_uploader
from streamlit_ui.components.report_downloader import (
    render_report_downloader,
    render_report_selector,
)
from tests.mock.mock_objects_streamlit import (
    MockCheckpointer,
    MockFileStore,
    MockAgentRunner,
)


@st.cache_resource
def _shared_mock_checkpointer() -> MockCheckpointer:
    """Shared in-memory checkpointer for mock mode.

    Cached so all sessions created during a session-of-the-app share one
    checkpointer - which is what makes saved chats listable and switchable
    from the session selector.
    """
    return MockCheckpointer()


@st.cache_resource
def _shared_mock_file_store() -> MockFileStore:
    """Shared in-memory file store for mock mode.

    Findings/reports are global by design (see upload_finding), so every
    service in mock mode writes to the same store.
    """
    return MockFileStore()


@st.cache_resource
def _shared_mock_agent_runner() -> MockAgentRunner:
    """Shared in-memory agent runner for mock mode."""
    return MockAgentRunner()


def _build_mock_service(thread_id: str) -> PenTestAgentService:
    """Build a service backed by in-memory mock dependencies.

    Used when STREAMLIT_USE_MOCKS=1 - lets you run the full UI without Postgres,
    an LLM, or a live agent. All state is lost between server restarts.
    """
    return PenTestAgentService(
        thread_id=thread_id,
        checkpointer=_shared_mock_checkpointer(),
        file_store=_shared_mock_file_store(),
        agent_runner=_shared_mock_agent_runner(),
    )


def _build_production_service(thread_id: str) -> PenTestAgentService:
    """Build a service wired to the real backend.

    TODO: implemented when the real adapters land. The production dependency
    source is agent.report_agent.AgentRuntime, which exposes `agent`,
    `checkpointer` and `store` after `await initialize()`. Those primitives need
    protocol adapters (CheckpointerProtocol / FileStoreProtocol /
    AgentRunnerProtocol) before they can be injected here. Until then, run with
    STREAMLIT_USE_MOCKS=1.
    """
    raise NotImplementedError(
        "Real backend wiring is not implemented yet. "
        "Run with STREAMLIT_USE_MOCKS=1 to use in-memory mock dependencies."
    )


def get_or_create_service(thread_id: str) -> PenTestAgentService:
    """Get or create PenTestAgentService instance for current thread.

    In mock mode (STREAMLIT_USE_MOCKS=1) the service is built from in-memory
    mocks. In production mode it is built from AgentRuntime-backed adapters
    (not wired yet - see _build_production_service).

    Args:
        thread_id: Current session/thread identifier

    Returns:
        Configured PenTestAgentService instance

    Data Flow:
        App startup → get_or_create_service() → Initialize dependencies → Cache in session_state
    """
    if "service" not in st.session_state or st.session_state.service.thread_id != thread_id:
        if os.environ.get("STREAMLIT_USE_MOCKS", "").lower() in ("1", "true", "yes"):
            service = _build_mock_service(thread_id)
        else:
            service = _build_production_service(thread_id)

        st.session_state.service = service

    return st.session_state.service


def render_sidebar(service: PenTestAgentService) -> None:
    """Render sidebar with session controls, file upload, and reports.
    
    Args:
        service: PenTestAgentService instance for operations
        
    Data Flow:
        Page load → render_sidebar() → Display UI elements → Handle user actions
    """
    with st.sidebar:
        st.title("🤖 Pen-Test Agent")
        
        # Session selection
        st.markdown("### 📋 Sessions")
        existing_sessions = service.list_threads()
        
        if existing_sessions:
            selected_session = st.selectbox(
                label="Select Session",
                options=existing_sessions,
                index=len(existing_sessions) - 1,  # Default to latest
                key="session_selector"
            )
            
            if selected_session != st.session_state.current_thread_id:
                st.session_state.current_thread_id = selected_session
                st.session_state.chat_messages = service.get_chat_history()
                st.rerun()
        
        if st.button("➕ New Session", use_container_width=True):
            import uuid
            new_thread_id = f"session_{uuid.uuid4().hex[:8]}"
            st.session_state.current_thread_id = new_thread_id
            st.session_state.chat_messages = []
            st.session_state.service = get_or_create_service(new_thread_id)
            st.rerun()
        
        st.divider()
        
        # File upload section
        def handle_upload(filename: str, content: bytes) -> None:
            """Callback for file upload component."""
            service.upload_finding(filename, content)
            st.session_state.findings = service.list_findings()
        
        render_file_uploader(handle_upload)
        
        # Display uploaded findings
        st.divider()
        st.markdown("### 📁 Uploaded Findings")
        st.session_state.findings = service.list_findings()
        
        if st.session_state.findings:
            for finding in st.session_state.findings:
                st.text(f"• {finding}")
        else:
            st.info("No findings uploaded")
        
        st.divider()
        
        # Reports section
        st.markdown("### 📄 Reports")
        selected_report = render_report_selector(service.list_reports)

        if selected_report:
            render_report_downloader(service.download_report, selected_report)


def render_main_chat(service: PenTestAgentService) -> None:
    """Render main chat area with message history and input.
    
    Args:
        service: PenTestAgentService instance for agent interaction
        
    Data Flow:
        User input → Append to chat_messages → service.send_message() 
        → Render streaming events → Save history
    """
    st.title("💬 Chat with Pen-Test Agent")
    
    # Display chat history
    for message in st.session_state.chat_messages:
        render_message(message["role"], message["content"])
    
    # Chat input
    if prompt := st.chat_input("Ask the agent to analyze findings..."):
        # Add user message
        st.session_state.chat_messages.append({"role": "user", "content": prompt})
        render_message("user", prompt)
        
        # Stream agent response
        with st.chat_message("assistant"):
            message_placeholder = st.empty()
            full_response = ""
            
            # Process streaming events
            for event in service.send_message(prompt):
                if event["type"] == "tool_call":
                    # Render tool call visualization
                    render_tool_call(
                        event["data"]["name"],
                        event["data"]["args"]
                    )
                elif event["type"] == "message":
                    # Accumulate final response
                    full_response = event["data"]
                    message_placeholder.write(full_response)
            
            # Add assistant message to history
            if full_response:
                st.session_state.chat_messages.append({
                    "role": "assistant",
                    "content": full_response
                })
                
                # Persist to database
                service.save_chat_history(st.session_state.chat_messages)


def main() -> None:
    """Main application entry point.
    
    Initializes session state and renders UI components.
    
    Data Flow:
        App start → Initialize session_state → render_sidebar() → render_main_chat()
    """
    st.set_page_config(
        page_title="Pen-Test Reporting Agent",
        page_icon="🤖",
        layout="wide",
    )
    
    # Initialize session state
    if "current_thread_id" not in st.session_state:
        import uuid
        st.session_state.current_thread_id = f"session_{uuid.uuid4().hex[:8]}"
    
    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []
    
    if "findings" not in st.session_state:
        st.session_state.findings = []
    
    # Get or create service for current thread
    service = get_or_create_service(st.session_state.current_thread_id)
    
    # Render UI
    render_sidebar(service)
    render_main_chat(service)


if __name__ == "__main__":
    main()
