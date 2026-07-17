"""
Streamlit UI Main Application

Main entry point for the pentest report agent Streamlit interface.
Provides session management, chat interface, and file operations.
"""

import streamlit as st
import asyncio
from typing import Optional

# Import service and components
from streamlit_ui.services.agent_service import PenTestAgentService
from streamlit_ui.components.chat_display import (
    render_chat_history,
    display_streaming_events
)
from streamlit_ui.components.file_uploader import (
    render_file_uploader,
    render_findings_list
)
from streamlit_ui.components.report_downloader import (
    render_report_downloader
)


# Page configuration
st.set_page_config(
    page_title="Pentest Report Agent",
    page_icon="🔍",
    layout="wide",
    initial_sidebar_state="expanded"
)


def get_or_create_service() -> PenTestAgentService:
    """
    Get or create PenTestAgentService in session state.
    
    Uses st.cache_resource to prevent re-initialization on reruns.
    In production mode, this will connect to real PostgreSQL.
    In test mode, mock objects can be injected via session state.
    
    Returns:
        PenTestAgentService instance
    """
    # Check if mock service was injected for testing
    if 'mock_service' in st.session_state:
        return st.session_state.mock_service
        
    # Production mode: create or retrieve cached service
    if 'agent_service' not in st.session_state:
        service = PenTestAgentService(thread_id="default_session")
        st.session_state.agent_service = service
        
        # Initialize async connections
        with st.spinner("Initializing agent connection..."):
            asyncio.run(service.initialize())
            
    return st.session_state.agent_service


def render_session_selector(service: PenTestAgentService) -> None:
    """
    Render session selection dropdown in sidebar.
    
    Allows users to switch between different pentest sessions.
    Session history is preserved per thread_id in PostgreSQL.
    
    Args:
        service: PenTestAgentService instance
    """
    st.sidebar.markdown("### 🔄 Session Management")
    
    # Current session display
    current_session = service.current_thread_id
    
    # Session selector
    selected_session = st.sidebar.text_input(
        "Session ID",
        value=current_session,
        help="Enter session ID to load or create a new session"
    )
    
    # Switch session if changed
    if selected_session != current_session:
        service.set_thread_id(selected_session)
        st.rerun()
        
    # New session button
    if st.sidebar.button("➕ New Session", use_container_width=True):
        import uuid
        new_session_id = str(uuid.uuid4())[:8]
        service.set_thread_id(new_session_id)
        st.rerun()
        
    st.sidebar.divider()
    st.sidebar.info(f"**Active Session:** `{current_session}`")


def render_chat_input(service: PenTestAgentService) -> None:
    """
    Render chat input area and handle agent execution.
    
    Displays chat history and processes user input.
    Streams agent responses with tool call visualization.
    
    Args:
        service: PenTestAgentService instance
    """
    st.markdown("### 💬 Agent Chat")
    
    # Load and display chat history
    with st.spinner("Loading conversation history..."):
        history = asyncio.run(service.get_session_history())
        
    if history:
        render_chat_history(history)
        st.divider()
    
    # Chat input form
    with st.form(key="chat_form", clear_on_submit=True):
        user_input = st.text_area(
            "Your message:",
            placeholder="Ask the agent to analyze findings, generate reports, etc.",
            height=100
        )
        
        submit_button = st.form_submit_button("🚀 Send", use_container_width=True)
        
        if submit_button and user_input:
            # Display user message
            st.info(f"**You:** {user_input}")
            
            # Create container for streaming events
            events_container = st.container()
            
            # Execute agent and stream events
            with st.spinner("Agent is thinking..."):
                event_stream = service.execute_agent(user_input)
                final_response = display_streaming_events(events_container, event_stream)
                
            if final_response:
                st.success("✅ Agent completed the task.")


def main():
    """Main application entry point."""
    
    # Initialize service
    service = get_or_create_service()
    
    # Sidebar components
    render_session_selector(service)
    render_file_uploader(service)
    render_findings_list(service)
    render_report_downloader(service)
    
    # Main chat area
    render_chat_input(service)
    
    # Footer
    st.markdown("---")
    st.caption(
        "Pentest Report Agent UI | "
        "Sessions persist in PostgreSQL | "
        "Findings are global across sessions"
    )


if __name__ == "__main__":
    main()
