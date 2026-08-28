"""Chat display component for rendering agent messages and tool calls.

This module provides functions to render conversation messages and visualize
agent tool calls in the Streamlit UI.

## Data Flow

```
Agent Event → render_tool_call() / render_message() → Streamlit UI Element
```

## Component Hierarchy

- render_message(): Displays user/assistant text messages
- render_tool_call(): Visualizes tool invocations with expandable details
"""

from typing import Dict, Any, Optional
import streamlit as st


def render_message(role: str, content: str) -> None:
    """Render a chat message with role-based styling.
    
    Args:
        role: Message role ('user', 'assistant', or 'system')
        content: Message text content
        
    Usage:
        ```python
        render_message("user", "Scan target 10.0.0.1")
        render_message("assistant", "Starting nmap scan...")
        ```
    """
    if role == "user":
        st.chat_message("user").write(content)
    elif role == "assistant":
        st.chat_message("assistant").write(content)
    else:
        st.chat_message("assistant").write(content)


def render_tool_call(tool_name: str, tool_args: Dict[str, Any]) -> None:
    """Render a tool call visualization with expandable details.
    
    Displays tool invocation in a formatted box showing:
    - Tool name (prominent)
    - Arguments (in expandable JSON viewer)
    
    Args:
        tool_name: Name of the tool being called (e.g., "nmap_scan")
        tool_args: Dictionary of arguments passed to the tool
        
    Visual Layout:
        ┌─────────────────────────────┐
        │ 🔧 Calling tool: nmap_scan  │
        │ ▼ Arguments                 │
        │   {                         │
        │     "target": "10.0.0.1"   │
        │   }                         │
        └─────────────────────────────┘
        
    Usage:
        ```python
        # From agent event stream
        for event in service.send_message("Scan target"):
            if event["type"] == "tool_call":
                render_tool_call(
                    event["data"]["name"],
                    event["data"]["args"]
                )
        ```
    """
    with st.expander(f"🔧 Calling tool: **{tool_name}**", expanded=False):
        st.json(tool_args)
