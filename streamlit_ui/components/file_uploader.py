"""File uploader component for uploading findings to virtual filesystem.

This module provides a reusable file upload widget that integrates with
the PenTestAgentService to store uploaded files in the global findings namespace.

## Data Flow

```
User selects file → render_file_uploader() → on_upload() → Virtual FS
```

## Behaviour

- After a successful upload the dropzone clears itself (rotating widget key).
- If a file with the same name already exists, a collision banner offers
  "Replace existing" / "Cancel". Duplicates are never silently accepted.
"""

import streamlit as st
from typing import Callable, Optional


def render_file_uploader(
    on_upload: Callable[[str, bytes, bool], Optional[str]],
    allowed_extensions: list[str] | None = None,
) -> None:
    """Render file upload widget with self-clearing and collision UX.

    Args:
        on_upload: Callback(filename, content, replace). ``replace=False`` on
            a fresh upload attempt, ``replace=True`` after the user chooses to
            overwrite an existing file. Returns a status message on success,
            ``"__COLLISION__"`` when the name already exists, or None.
        allowed_extensions: Optional list of allowed extensions.
    """
    upload_gen = st.session_state.setdefault("_upload_gen", 0)
    upload_key = f"finding_uploader_{upload_gen}"

    pending = st.session_state.get("_pending_collision")

    # Show a one-shot status message left by the previous run (after rerun).
    if msg := st.session_state.pop("_last_upload_message", None):
        st.success(msg)

    # --- Pending collision decision banner --------------------------------
    if pending is not None:
        st.warning(
            f"File **`{pending['filename']}`** already exists in findings. "
            "Duplicates are not silently accepted."
        )
        col1, col2 = st.columns(2)
        if col1.button("Replace existing", key="collision_replace"):
            status = on_upload(
                pending["filename"], pending["content"], replace=True
            )
            st.session_state["_pending_collision"] = None
            st.session_state["_upload_gen"] = upload_gen + 1
            if status:
                st.session_state["_last_upload_message"] = status
            st.rerun()
        if col2.button("Cancel", key="collision_cancel"):
            st.session_state["_pending_collision"] = None
            st.session_state["_upload_gen"] = upload_gen + 1
            st.rerun()

    st.markdown("### 📁 Upload Finding")

    uploaded_file = st.file_uploader(
        label="Choose a findings file",
        type=allowed_extensions,
        help="Upload reconnaissance data (nmap scans, bloodhound dumps, etc.)",
        key=upload_key,
    )

    # --- Fresh upload handling --------------------------------------------
    if pending is None and uploaded_file is not None:
        content = uploaded_file.getvalue()
        filename = uploaded_file.name

        status = on_upload(filename, content, replace=False)

        if status == "__COLLISION__":
            # Stage the collision so the banner renders on the next run.
            st.session_state["_pending_collision"] = {
                "filename": filename,
                "content": content,
            }
            st.rerun()
        elif status:
            # Success – clear the dropzone and stash the message for display
            # after the rerun.
            st.session_state["_last_upload_message"] = status
            st.session_state["_upload_gen"] = upload_gen + 1
            st.rerun()
