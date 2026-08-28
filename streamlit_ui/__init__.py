"""Streamlit UI package for pen-test reporting agent.

This package provides a web interface for interacting with the penetration
test reporting agent, featuring:
- Session management with conversation history persistence
- Findings file upload to global virtual filesystem
- Real-time agent tool call visualization
- Report download from virtual filesystem
"""

from streamlit_ui.components import (
    render_message,
    render_tool_call,
    render_file_uploader,
    render_report_downloader,
)
from streamlit_ui.services.agent_service import PenTestAgentService

__all__ = [
    "render_message",
    "render_tool_call",
    "render_file_uploader",
    "render_report_downloader",
    "PenTestAgentService",
]
