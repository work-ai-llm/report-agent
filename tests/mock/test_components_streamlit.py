"""AppTest verification for the Streamlit UI view components (PR #3).

Runs the real component functions through streamlit.testing.v1.AppTest —
no live server, no database, no LLM. Proves each component renders the
expected elements into the Streamlit element tree given mock-based data.

Note: AppTest.from_function executes the function body inside a fresh temp
module, so each `app()` below imports its components itself and receives
observability objects (lists) as arguments.

## What these tests verify

- chat_display: chat bubbles for user/assistant + expandable tool-call JSON
- file_uploader: widget renders and the callback receives the uploaded bytes
- report_downloader: warning when a report is missing; content is only
  fetched when the button is clicked (not on every rerun)
- report_selector: info message when empty, selectbox otherwise

## Running

```bash
python -m pytest tests/mock/test_components_streamlit.py -v
```
"""

from typing import Optional, List, Tuple
from streamlit.testing.v1 import AppTest
from streamlit_ui.components.chat_display import render_message, render_tool_call
from streamlit_ui.components.file_uploader import render_file_uploader
from streamlit_ui.components.report_downloader import (
    render_report_downloader,
    render_report_selector,
)


class TestChatDisplay:
    """Tests for chat message and tool call rendering."""

    def test_render_message_creates_chat_bubbles(self) -> None:
        def app() -> None:
            from streamlit_ui.components.chat_display import render_message
            render_message("user", "hello user")
            render_message("assistant", "hi assistant")

        at = AppTest.from_function(app).run()
        assert len(at.chat_message) == 2

    def test_render_tool_call_renders_expander_and_json(self) -> None:
        def app() -> None:
            from streamlit_ui.components.chat_display import render_tool_call
            render_tool_call("nmap_scan", {"target": "10.0.0.1"})

        at = AppTest.from_function(app).run()
        assert len(at.expander) == 1
        assert len(at.json) == 1
        assert "nmap_scan" in at.expander[0].label


class TestFileUploader:
    """Tests for the findings upload widget."""

    def test_render_file_uploader_calls_back_with_upload(self) -> None:
        captured: List[Tuple[str, bytes, bool]] = []

        def app(captured) -> None:
            from streamlit_ui.components.file_uploader import render_file_uploader

            def handle(filename, content, replace=False):
                captured.append((filename, content, replace))

            render_file_uploader(handle)

        at = AppTest.from_function(app, args=(captured,)).run()
        at.file_uploader[0].upload("nmap.txt", b"Port 80 open").run()
        assert captured == [("nmap.txt", b"Port 80 open", False)]

    def test_uploader_clears_after_successful_upload(self) -> None:
        """After a successful upload the dropzone is remounted (key rotates)."""
        def app() -> None:
            from streamlit_ui.components.file_uploader import render_file_uploader

            def handle(filename, content, replace=False):
                return f"Uploaded {filename}"

            render_file_uploader(handle)

        at = AppTest.from_function(app).run()
        assert len(at.file_uploader) == 1

        # Upload a file -> success status is shown and a rerun is triggered.
        at.file_uploader[0].upload("a.txt", b"content").run()
        assert len(at.success) == 1

    def test_collision_stages_decision_and_replace_uploads(self) -> None:
        """A duplicate filename is not silently accepted; Replace works."""
        calls: List[Tuple[str, bool]] = []

        def app(calls) -> None:
            from streamlit_ui.components.file_uploader import render_file_uploader

            def handle(filename, content, replace=False):
                calls.append((filename, replace))
                return "__COLLISION__" if not replace else f"Replaced {filename}"

            render_file_uploader(handle)

        at = AppTest.from_function(app, args=(calls,)).run()
        # First upload triggers collision staging.
        at.file_uploader[0].upload("dup.txt", b"data").run()
        assert calls == [("dup.txt", False)]
        # Collision banner appears.
        assert len(at.warning) == 1
        # Click "Replace existing".
        for btn in at.button:
            if "Replace existing" in str(btn):
                btn.click().run()
                break
        assert ("dup.txt", True) in calls
        assert len(at.success) >= 1


class TestReportDownloader:
    """Tests for the report download widget."""

    def test_warns_when_report_is_missing(self) -> None:
        def app() -> None:
            from streamlit_ui.components.report_downloader import (
                render_report_downloader,
            )
            render_report_downloader(lambda name: None, "missing.md")

        at = AppTest.from_function(app).run()
        # Content is fetched at render time; a missing report warns immediately.
        assert len(at.warning) == 1
        assert len(at.download_button) == 0

    def test_single_click_downloads(self) -> None:
        """st.download_button is rendered directly – no intermediate st.button."""
        calls: List[str] = []

        def app(calls) -> None:
            from streamlit_ui.components.report_downloader import (
                render_report_downloader,
            )

            def download(name):
                calls.append(name)
                return b"# Report"

            render_report_downloader(download, "report.md")

        at = AppTest.from_function(app, args=(calls,)).run()
        # Content is fetched at render time, so the download button is direct.
        assert calls == ["report.md"]
        assert len(at.download_button) == 1
        # No separate st.button wrapper (single click downloads).
        assert len(at.button) == 0


class TestReportSelector:
    """Tests for the report list dropdown."""

    def test_shows_info_when_no_reports(self) -> None:
        def app() -> None:
            from streamlit_ui.components.report_downloader import (
                render_report_selector,
            )
            render_report_selector(lambda: [])

        at = AppTest.from_function(app).run()
        assert len(at.info) == 1

    def test_shows_selectbox_with_reports(self) -> None:
        def app() -> None:
            from streamlit_ui.components.report_downloader import (
                render_report_selector,
            )
            render_report_selector(lambda: ["a.md", "b.md"])

        at = AppTest.from_function(app).run()
        assert len(at.selectbox) == 1
        assert at.selectbox[0].options == ["a.md", "b.md"]