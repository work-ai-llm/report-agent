"""Filesystem viewer components for the agent's two storage backends.

The agent's virtual filesystem is not one thing. Files under ``/context/`` are
persisted in the store; everything else - the scratchpad the agent drafts into -
is held by an in-memory backend in the agent's own checkpoint namespace. A store
listing returns the first half only, so these two panels exist as a pair: one
reads the store, the other reads the state channel.

Like file_uploader.py and report_downloader.py, these components receive their
data as arguments rather than depending on the concrete PenTestAgentService, so
the view layer stays decoupled from the service implementation.

## Usage Pattern

```python
render_filesystem_view(service.list_filesystem())
render_scratchpad_view(service.list_scratchpad())
```
"""

from typing import Dict, List
import streamlit as st

_CONTEXT_ROOT = "/context/"
# Paths arrive with the leading slash already stripped, so the comparison and
# the slice both have to use the bare form. Mixing the two silently ate the
# first character of the top-level directory name.
_CONTEXT_ROOT_BARE = _CONTEXT_ROOT.lstrip("/")
_PREVIEW_CHARS = 400


def _group_by_directory(paths: List[str]) -> Dict[str, List[str]]:
    """Group display paths by their directory, keeping the directory readable.

    ``/context/findings/nmap.txt`` lands under ``findings``. Nested paths keep
    their remaining path so a file three levels down does not lose its shape.
    """
    grouped: Dict[str, List[str]] = {}
    for path in paths:
        bare = path.lstrip("/")
        if bare.startswith(_CONTEXT_ROOT_BARE):
            bare = bare[len(_CONTEXT_ROOT_BARE):]
        if "/" not in bare:
            grouped.setdefault("(root)", []).append(bare)
            continue
        directory, _, name = bare.rpartition("/")
        grouped.setdefault(directory, []).append(name)
    return grouped


def render_filesystem_view(paths: List[str]) -> None:
    """Render every persisted file the agent can read, grouped by directory.

    Shows the whole of the store-backed filesystem rather than a hand-picked
    slice, so the context library the agent is told to read is visible alongside
    the findings that were uploaded and the reports that came back. Paths are
    expected in display form (``/context/findings/nmap.txt``).

    Args:
        paths: Sorted display paths from ``service.list_filesystem()``
    """
    st.markdown("### 🗂 Agent Filesystem")

    if not paths:
        st.info("Nothing in the virtual filesystem yet")
        return

    grouped = _group_by_directory(paths)
    for directory in sorted(grouped):
        st.markdown(f"**`{_CONTEXT_ROOT}{directory}/`**")
        for name in sorted(grouped[directory]):
            st.text(f"• {name}")


def render_scratchpad_view(files: Dict[str, str]) -> None:
    """Render the agent's working files for the current session.

    These are the drafts and intermediate notes the agent writes while it
    works. They are the only visible sign of its process, since the reasoning
    itself never reaches the UI. Content is collapsed by default: it is the
    agent's own commentary over the operator's findings, and it is long.

    Args:
        files: Mapping of path to text content from ``service.list_scratchpad()``
    """
    st.markdown("### 📝 Scratchpad")

    if not files:
        st.caption("Empty — the agent has not written working files yet.")
        return

    for path in sorted(files):
        content = files[path] or ""
        with st.expander(f"`{path}` — {len(content)} chars"):
            if len(content) > _PREVIEW_CHARS:
                st.caption(f"First {_PREVIEW_CHARS} characters:")
                st.text(content[:_PREVIEW_CHARS])
                st.caption(f"… {len(content) - _PREVIEW_CHARS} more characters")
            else:
                st.text(content)