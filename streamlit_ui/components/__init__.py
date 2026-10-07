"""Streamlit UI components package.

This package provides reusable UI building blocks for the Streamlit pen-test UI:
- chat_display.py: Render agent messages and tool calls
- file_uploader.py: Upload findings to virtual filesystem
- report_downloader.py: Download reports from virtual filesystem
- filesystem_view.py: Show the agent's store-backed files and its scratchpad
"""

from streamlit_ui.components.chat_display import render_message, render_tool_call
from streamlit_ui.components.file_uploader import render_file_uploader
from streamlit_ui.components.report_downloader import (
    render_report_downloader,
    render_report_selector,
)
from streamlit_ui.components.filesystem_view import (
    render_filesystem_view,
    render_scratchpad_view,
)

__all__ = [
    "render_message",
    "render_tool_call",
    "render_file_uploader",
    "render_report_downloader",
    "render_report_selector",
    "render_filesystem_view",
    "render_scratchpad_view",
]
