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
- render_chat_history(): Renders a full conversation from persisted history
"""

from typing import Dict, Any, List, Optional
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


def render_tool_call(tool_name: str, tool_args: Dict[str, Any], tool_result: Optional[str] = None) -> None:
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
        if tool_result:
            st.markdown("**Result:**")
            st.code(str(tool_result), language=None)


def render_reasoning(reasoning: Optional[str]) -> None:
    """Render a model's reasoning as dimmed text above the answer it produced.

    One utterance, not two: the reasoning and the answer it led to are the same
    message, so they sit in the same assistant bubble with the thinking first.
    That keeps reading order intact - you see the model work, then its conclusion -
    rather than having to open a separate panel and remember what it said.

    ``st.caption`` rather than hand-written HTML: it is the dimmed text style
    Streamlit already ships, so it follows the active theme in both light and dark
    instead of hardcoding a grey that disappears on one of them. Reasoning is
    frequently longer than the answer, and a caption is visually subordinate
    without being hidden, which is the balance wanted here.

    Absent when the backend did not surface the field - the local model needs
    ``ReasoningChatOpenAI`` to keep it at all (#18).

    Args:
        reasoning: Reasoning text, or None/empty when the model did not expose
            any.
    """
    if not reasoning or not reasoning.strip():
        return
    st.caption(reasoning.strip())


def render_chat_history(messages: List[Dict[str, Any]]) -> None:
    """Render a full conversation from persisted history entries.

    Each entry has a ``type`` field that determines how it is rendered:

    - ``"user"`` → user chat bubble with ``content``
    - ``"assistant"`` → assistant chat bubble with ``content``
    - ``"tool_call"`` → expandable tool-call block (``name`` + ``args``)

    Consecutive ``tool_call`` entries are grouped with the following
    ``assistant`` entry inside a single assistant chat bubble, matching
    the live-streaming layout.

    Args:
        messages: Flat list of history entries in chronological order.
    """
    i = 0
    while i < len(messages):
        entry = messages[i]
        etype = entry.get("type", "")

        if etype == "user":
            render_message("user", entry["content"])
            i += 1

        elif etype in ("assistant", "tool_call"):
            # Collect a group: leading tool_calls + one assistant message
            with st.chat_message("assistant"):
                while i < len(messages) and messages[i].get("type") == "tool_call":
                    render_tool_call(
                        messages[i]["name"],
                        messages[i]["args"],
                        messages[i].get("result"),
                    )
                    i += 1
                if i < len(messages) and messages[i].get("type") == "assistant":
                    render_reasoning(messages[i].get("reasoning"))
                    st.write(messages[i]["content"])
                    i += 1

        else:
            # Unknown entry type – render as assistant text to avoid data loss
            render_message("assistant", entry.get("content", str(entry)))
            i += 1
