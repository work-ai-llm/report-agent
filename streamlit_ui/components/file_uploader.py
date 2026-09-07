"""File uploader component for uploading findings to virtual filesystem.

This module provides a reusable file upload widget that integrates with
the PenTestAgentService to store uploaded files in the global findings namespace.

## Data Flow

```
User selects file → render_file_uploader() → service.upload_finding() → Virtual FS
```

## Usage Pattern

```python
def handle_upload(filename: str, content: bytes):
    service.upload_finding(filename, content)
    st.success(f"Uploaded {filename}")

render_file_uploader(handle_upload)
```
"""

from typing import Callable, Optional
import streamlit as st


def render_file_uploader(
    on_upload: Callable[[str, bytes], Optional[str]],
    allowed_extensions: list[str] | None = None
) -> None:
    """Render file upload widget for findings.
    
    Displays a file uploader that accepts findings files and calls the
    provided callback with filename and content bytes. The callback returns
    an optional status message that is rendered under the widget (return
    None when the file has already been handled, e.g. re-uploaded on a rerun,
    to avoid spamming status messages on every interaction).

    Args:
        on_upload: Callback function(filename: str, content: bytes) -> Optional[str]
                   to handle the uploaded file and return a status message
        allowed_extensions: Optional list of allowed file extensions
                           (e.g., ['.txt', '.md', '.json'])
                           
    Visual Layout:
        ┌─────────────────────────────┐
        │ 📁 Upload Finding           │
        │ [Browse...]                 │
        │                             │
        │ ✓ nmap_scan.txt uploaded    │
        └─────────────────────────────┘
        
    Usage:
        ```python
        # In sidebar
        def handle_upload(filename: str, content: bytes) -> Optional[str]:
            if filename in service.list_findings():
                return f"{filename} already exists - replaced it."
            service.upload_finding(filename, content)
            return f"Uploaded {filename}"
        
        render_file_uploader(handle_upload)
        ```
    """
    st.markdown("### 📁 Upload Finding")
    
    # Fixed widget key: id(on_upload) changes every rerun (new closure each time),
    # which would remount the widget and lose any uploaded file between runs.
    uploaded_file = st.file_uploader(
        label="Choose a findings file",
        type=allowed_extensions,
        help="Upload reconnaissance data (nmap scans, bloodhound dumps, etc.)",
        key="finding_uploader"
    )
    
    if uploaded_file is not None:
        # Read file content as bytes
        content = uploaded_file.getvalue()
        filename = uploaded_file.name
        
        # Call the handler to store in virtual FS; it may return a status message
        status = on_upload(filename, content)
        if status:
            st.success(status)
