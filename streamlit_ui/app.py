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
from typing import Optional, Callable, List, Dict, Any
from streamlit_ui.services.agent_service import PenTestAgentService
from streamlit_ui.components.chat_display import (
    render_chat_history,
    render_message,
    render_tool_call,
)
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
    service in mock mode writes to the same store. A demo report is seeded so
    the download path can be exercised without a real agent run.
    """
    store = MockFileStore()
    store.prepopulate_with_report(
        "demo_pentest_report.md",
        b"# Demo Pentest Report\n\nSeeded by mock mode so the store + download "
        b"path can be tested without a real agent run.\n",
    )
    store.prepopulate_with_report(
        "demo_pentest_report_2.md",
        b"# Demo check 2\n\nSeeded by mock mode so the store + download "
        b"\n I want to check how download selector works.",
    )
    
    return store


@st.cache_resource
def _shared_mock_agent_runner() -> MockAgentRunner:
    """Shared in-memory agent runner for mock mode.

    Pre-loaded with plausible tool calls and a small per-event delay so the
    chat visibly streams (tool call blocks, then the reply) like a real run.
    """
    runner = MockAgentRunner(delay_seconds=0.35)
    runner.set_tool_calls([
        {"name": "nmap_scan", "args": {"target": "10.0.0.1"}},
        {"name": "nikto_scan", "args": {"url": "http://10.0.0.1"}},
    ])
    return runner


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

        selected_session = None
        if existing_sessions:
            selected_session = st.selectbox(
                label="Select Session",
                options=existing_sessions,
                key="session_selector",
            )

        def _new_session():
            import uuid
            new_thread_id = f"session_{uuid.uuid4().hex[:8]}"
            st.session_state.current_thread_id = new_thread_id
            st.session_state.chat_messages = []
            st.session_state._service_thread = None
            svc = get_or_create_service(new_thread_id)
            svc.save_chat_history([])
            st.session_state["session_selector"] = new_thread_id

        st.button(
            "➕ New Session",
            on_click=_new_session,
            use_container_width=True,
        )

        if selected_session and selected_session != st.session_state.current_thread_id:
            st.session_state.current_thread_id = selected_session
            st.session_state.chat_messages = []
            st.session_state._service_thread = None
        
        st.divider()
        
        # File upload section
        def handle_upload(
            filename: str, content: bytes, replace: bool = False
        ) -> Optional[str]:
            """Callback for file upload component.

            Returns ``"__COLLISION__"`` when the file name already exists and
            the user has not yet chosen to replace it. On success returns a
            status message. Returns None when the file has already been
            processed this session (dedupe).

            Args:
                filename: Uploaded file name.
                content: Uploaded file bytes.
                replace: True when the user chose to overwrite an existing file.
            """
            if filename in service.list_findings() and not replace:
                return "__COLLISION__"

            if filename in st.session_state.processed_findings:
                return None

            service.upload_finding(filename, content)
            st.session_state.processed_findings.add(filename)
            st.session_state.findings = service.list_findings()
            if replace:
                return f"Replaced `{filename}` with the new upload."
            return f"Uploaded `{filename}` ({len(content)} bytes)."
        
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


def _history_from_stored(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Convert stored history entries into a flat event list.

    The stored history (from checkpointer) may contain plain role/content
    entries (written before structured events were introduced) or structured
    events (``{"type": ...}``). This helper normalises the former into the
    structured form so ``render_chat_history`` can render either.

    Args:
        messages: Entries as stored in the checkpointer.

    Returns:
        Normalised flat event list.
    """
    normalised: List[Dict[str, Any]] = []
    for m in messages:
        if "type" in m:
            normalised.append(m)
        elif m.get("role") == "user":
            normalised.append({"type": "user", "content": m["content"]})
        elif m.get("role") == "assistant":
            normalised.append({"type": "assistant", "content": m["content"]})
        else:
            normalised.append({"type": "assistant", "content": str(m)})
    return normalised


def render_main_chat(service: PenTestAgentService) -> None:
    """Render main chat area with message history and input.
    
    Args:
        service: PenTestAgentService instance for agent interaction
        
    Data Flow:
        User input → Append to chat_messages → service.send_message() 
        → Render streaming events → Save history
    """
    st.title("💬 Chat with Pen-Test Agent")
    
    # Display chat history from the structured event list
    render_chat_history(
        _history_from_stored(st.session_state.chat_messages)
    )
    
    # Chat input
    if prompt := st.chat_input("Ask the agent to analyze findings..."):
        # Create session on first message (not on page load)
        if st.session_state.current_thread_id is None:
            import uuid
            st.session_state.current_thread_id = f"session_{uuid.uuid4().hex[:8]}"
            service = get_or_create_service(st.session_state.current_thread_id)
            service.save_chat_history([])

        # Add user message
        st.session_state.chat_messages.append({"type": "user", "content": prompt})
        render_message("user", prompt)
        
        # Stream agent response
        with st.chat_message("assistant"):
            # Render tool-call blocks first, then accumulate the final text
            # below them so the answer text appears underneath the expanders.
            tool_events: List[Dict[str, Any]] = []
            full_response = ""
            
            # Process streaming events
            for event in service.send_message(prompt):
                if event["type"] == "tool_call":
                    name = event["data"]["name"]
                    args = event["data"]["args"]
                    tool_events.append({"type": "tool_call", "name": name, "args": args})
                    render_tool_call(name, args)
                elif event["type"] == "message":
                    full_response = event["data"]
                    st.write(full_response)
            
            # Persist tool calls + assistant reply as structured entries
            if tool_events or full_response:
                st.session_state.chat_messages.extend(tool_events)
                if full_response:
                    st.session_state.chat_messages.append({
                        "type": "assistant",
                        "content": full_response,
                    })
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
    
    # Session is only created when the first message is sent (not on page load).
    if "current_thread_id" not in st.session_state:
        st.session_state.current_thread_id = None

    if "chat_messages" not in st.session_state:
        st.session_state.chat_messages = []

    if "findings" not in st.session_state:
        st.session_state.findings = []

    if "processed_findings" not in st.session_state:
        st.session_state.processed_findings = set()

    if "_service_thread" not in st.session_state:
        st.session_state._service_thread = None

    # Sidebar needs a service to list existing sessions even before a thread
    # is active.  Use the latest existing thread or a placeholder.
    if st.session_state.current_thread_id is not None:
        service = get_or_create_service(st.session_state.current_thread_id)
    else:
        tmp_threads = get_or_create_service("___placeholder___").list_threads()
        tmp_id = tmp_threads[-1] if tmp_threads else "___placeholder___"
        service = get_or_create_service(tmp_id)

    # Render sidebar — its handlers may change the active thread
    render_sidebar(service)

    # If a New Session / session switch happened above, rebind the service for
    # the new thread and load that thread's saved history into the UI.
    if (
        st.session_state.current_thread_id is not None
        and st.session_state._service_thread != st.session_state.current_thread_id
    ):
        service = get_or_create_service(st.session_state.current_thread_id)
        st.session_state.chat_messages = service.get_chat_history()
        st.session_state._service_thread = st.session_state.current_thread_id

    # Render UI
    render_main_chat(service)


if __name__ == "__main__":
    main()
