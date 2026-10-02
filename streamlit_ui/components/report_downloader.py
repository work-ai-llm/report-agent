"""Report downloader component for downloading reports from virtual filesystem.

This module provides a reusable download widget that retrieves reports from
the global virtual filesystem and triggers browser downloads.

Like file_uploader.py, components receive data access as callbacks instead of
depending on the concrete PenTestAgentService. This keeps the view layer
decoupled from the service implementation.

## Data Flow

```
User clicks download → st.download_button → download_report() → Browser
```

## Usage Pattern

```python
# In sidebar or main area
render_report_selector(service.list_reports)
render_report_downloader(service.download_report, "pentest_report.md")
```
"""

from typing import Optional, List, Callable
import streamlit as st


def render_report_downloader(
    download_report: Callable[[str], Optional[bytes]],
    filename: str,
    label: Optional[str] = None,
) -> None:
    """Render a single-click download button for a specific report.

    The report content is fetched at render time so ``st.download_button``
    can be shown directly – one click triggers the download. A missing
    report shows a warning instead.

    Args:
        download_report: Callable(filename: str) -> Optional[bytes] that loads
                         the report content from the virtual filesystem
        filename: Name of the report file (e.g., "pentest_report.md")
        label: Optional custom button label (defaults to "📄 Download {filename}")
    """
    content = download_report(filename)
    if content is None:
        st.warning(f"Report **{filename}** not found")
        return

    button_label = label if label else f"📄 Download **{filename}**"
    st.download_button(
        label=button_label,
        data=content,
        file_name=filename,
        mime="text/markdown" if filename.endswith(".md") else "text/plain",
        key=f"save_report_{filename}",
    )


def render_report_selector(list_reports: Callable[[], List[str]]) -> Optional[str]:
    """Render dropdown selector for available reports.

    Displays a dropdown menu with all available reports and returns the
    selected filename. Use this with render_report_downloader to allow
    users to choose which report to download.

    Args:
        list_reports: Callable() -> List[str] that lists report filenames

    Returns:
        Selected filename or None if no selection

    Visual Layout:
        ┌─────────────────────────────┐
        │ Select Report:              │
        │ [pentest_report.md      ▼] │
        └─────────────────────────────┘

    Usage:
        ```python
        # In sidebar
        selected = render_report_selector(service.list_reports)
        if selected:
            render_report_downloader(service.download_report, selected)
        ```
    """
    reports = list_reports()

    if not reports:
        st.info("No reports available yet")
        return None

    # Create dropdown with report options
    selected = st.selectbox(
        label="Select Report",
        options=reports,
        key="report_selector",
    )

    return selected
