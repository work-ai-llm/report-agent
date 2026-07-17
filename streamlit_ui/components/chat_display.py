"""
Streamlit Chat Display Component

Renders agent messages with tool call visualization in the chat interface.
Displays streaming events from PenTestAgentService execution.
"""

import streamlit as st
from typing import List, Dict, Any
from dataclasses import dataclass


def render_tool_call(tool_name: str, args: Dict[str, Any], preview_args: Dict[str, Any] = None) -> None:
    """
    Render a tool call event in the chat interface.
    
    Args:
        tool_name: Name of the tool being called
        args: Full tool arguments
        preview_args: Optional preview of arguments (for large content)
    """
    display_args = preview_args if preview_args else args
    
    with st.expander(f"🔧 Agent calling tool: `{tool_name}`", expanded=False):
        st.json(display_args)
        

def render_message(content: str, message_type: str) -> None:
    """
    Render an agent or user message in the chat interface.
    
    Args:
        content: Message content
        message_type: Type of message ('ai', 'user', 'tool_result')
    """
    if message_type == 'ai':
        st.markdown(content)
    elif message_type == 'user':
        st.info(f"**You:** {content}")
    elif message_type == 'tool_result':
        st.success(f"✅ {content}")


def render_chat_history(messages: List[Dict[str, Any]]) -> None:
    """
    Render full chat history for session persistence.
    
    Args:
        messages: List of message dicts with 'role' and 'content'
    """
    for msg in messages:
        role = msg.get('role', 'assistant')
        content = msg.get('content', '')
        
        if role == 'user':
            render_message(content, 'user')
        elif role == 'assistant':
            render_message(content, 'ai')
        elif role == 'tool':
            render_message(f"Tool result: {content}", 'tool_result')


def display_streaming_events(events_container, event_stream) -> str:
    """
    Display streaming events from agent execution.
    
    Args:
        events_container: Streamlit container to render events in
        event_stream: Async generator of (event_type, event_data) tuples
        
    Returns:
        Final response text from agent
    """
    final_response = ""
    
    with events_container:
        for event_type, event_data in event_stream:
            if event_type == 'tool_call':
                render_tool_call(
                    tool_name=event_data.tool_name,
                    args=event_data.args,
                    preview_args=event_data.preview_args
                )
            elif event_type == 'message':
                render_message(event_data.content, event_data.message_type)
                if event_data.message_type == 'ai':
                    final_response = event_data.content
            elif event_type == 'error':
                st.error(f"❌ Error: {event_data}")
                
    return final_response
