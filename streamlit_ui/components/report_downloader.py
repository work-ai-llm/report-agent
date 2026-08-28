"""Report downloader component for downloading reports from virtual filesystem.

This module provides a reusable download widget that retrieves reports from
the global virtual filesystem and triggers browser downloads.

## Data Flow

```
User clicks download → render_report_downloader() → service.download_report() → Browser
```

## Usage Pattern

```python
# In sidebar or main area
render_report_downloader(service, "pentest_report.md")
```
"""

from typing import Optional
import streamlit as st
from streamlit_ui.services.agent_service import PenTestAgentService


def render_report_downloader(
    service: PenTestAgentService,
    filename: str,
    label: Optional[str] = None
) -> None:
    """Render download button for a specific report.
    
    Creates a button that when clicked retrieves the report from the virtual
    filesystem and triggers a browser download.
    
    Args:
        service: PenTestAgentService instance for file retrieval
        filename: Name of the report file (e.g., "pentest_report.md")
        label: Optional custom button label (defaults to "📄 Download {filename}")
        
    Visual Layout:
        ┌─────────────────────────────┐
        │ 📄 Download pentest_report  │
        │   .md                       │
        └─────────────────────────────┘
        
    Usage:
        ```python
        # Show download button if report exists
        reports = service.list_reports()
        if "pentest_report.md" in reports:
            render_report_downloader(service, "pentest_report.md")
        ```
    """
    default_label = f"📄 Download **{filename}**"
    button_label = label if label else default_label
    
    # Retrieve report content
    content = service.download_report(filename)
    
    if content is None:
        st.warning(f"Report **{filename}** not found")
        return
    
    # Create download button with file content
    st.download_button(
        label=button_label,
        data=content,
        file_name=filename,
        mime="text/markdown" if filename.endswith(".md") else "text/plain",
        key=f"download_{filename}"
    )


def render_report_selector(service: PenTestAgentService) -> Optional[str]:
    """Render dropdown selector for available reports.
    
    Displays a dropdown menu with all available reports and returns the
    selected filename. Use this with render_report_downloader to allow
    users to choose which report to download.
    
    Args:
        service: PenTestAgentService instance for listing reports
        
    Returns:
        Selected filename or None if no selection
        
    Visual Layout:
        ┌─────────────────────────────┐
        │ Select Report:              │
        │ [pentest_report.md      ▼] │
        │                             │
        │ 📄 Download Selected Report │
        └─────────────────────────────┘
        
    Usage:
        ```python
        # In sidebar
        selected = render_report_selector(service)
        if selected:
            render_report_downloader(service, selected)
        ```
    """
    reports = service.list_reports()
    
    if not reports:
        st.info("No reports available yet")
        return None
    
    # Create dropdown with report options
    selected = st.selectbox(
        label="Select Report",
        options=reports,
        key="report_selector"
    )
    
    return selected
