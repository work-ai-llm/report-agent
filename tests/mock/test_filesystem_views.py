"""Tests for the two sidebar filesystem panels: the store-backed filesystem
view and the agent's scratchpad view.

The two read different storage. ``list_filesystem`` goes through the file
store, which holds everything routed through ``/context/``. ``list_scratchpad``
goes through the checkpointer, because the agent's in-memory backend keeps its
files in a ``files`` state channel in the agent's own checkpoint namespace and
they are unreachable from a store listing at any prefix.

## What these tests verify

- The filesystem view lists the whole store, not a hand-picked slice, so the
  context library shows up alongside findings and reports.
- Display paths carry the ``/context/`` root the agent itself uses, even though
  the store strips it before writing.
- The scratchpad reads the agent namespace, is filtered to the scratchpad
  prefix, and is tolerant of leading slashes.
- Only a leading prefix is stripped when listing findings and reports. The
  previous ``str.replace`` also rewrote the same text mid-name, so
  ``findings/findings_summary.txt`` displayed as ``summary.txt`` and a later
  write using that displayed name targeted a different key.
- Directory grouping for display keeps nested paths readable.

## Running

```bash
python -m pytest tests/mock/test_filesystem_views.py -v
```

## What These Tests Do and Don't Prove

These use the in-memory mocks, which are permissive dictionaries. They prove the
service layer's path handling, prefix filtering and thread isolation - not the
real adapters. ``list_agent_files`` on ``RealCheckpointerAdapter`` reads the
``files`` channel out of a real checkpoint tuple and is validated separately by
``test_real_backend.py``; the mocks cannot prove that channel's shape.
"""

from streamlit_ui.services.agent_service import PenTestAgentService
from streamlit_ui.components.filesystem_view import _group_by_directory
from tests.mock.mock_objects_streamlit import (
    MockCheckpointer,
    MockFileStore,
    MockAgentRunner,
)


def _service(
    thread_id: str = "session_1",
    file_store: MockFileStore = None,
    checkpointer: MockCheckpointer = None,
) -> PenTestAgentService:
    return PenTestAgentService(
        thread_id=thread_id,
        checkpointer=checkpointer or MockCheckpointer(),
        file_store=file_store or MockFileStore(),
        agent_runner=MockAgentRunner(),
    )


class TestListFilesystem:
    """The store-backed panel shows everything the agent can read."""

    def test_lists_every_stored_file_not_just_findings_and_reports(self):
        store = MockFileStore()
        store.write_file("findings/nmap.txt", b"Port 80 open")
        store.write_file("reports/final.md", b"# Report")
        store.write_file("templates/recon_report_template.md", b"# Template")
        store.write_file("interpretations/enum.md", b"# Enum guide")

        paths = _service(file_store=store).list_filesystem()

        assert paths == [
            "/context/findings/nmap.txt",
            "/context/interpretations/enum.md",
            "/context/reports/final.md",
            "/context/templates/recon_report_template.md",
        ]

    def test_empty_store_returns_empty_list(self):
        assert _service().list_filesystem() == []

    def test_display_path_restores_the_context_root(self):
        """The agent says /context/findings/x when it lists; the panel must match."""
        store = MockFileStore()
        store.write_file("findings/nmap.txt", b"x")

        assert _service(file_store=store).list_filesystem() == [
            "/context/findings/nmap.txt"
        ]

    def test_tolerates_store_keys_written_with_leading_slash(self):
        store = MockFileStore()
        store.write_file("/findings/nmap.txt", b"x")

        assert _service(file_store=store).list_filesystem() == [
            "/context/findings/nmap.txt"
        ]


class TestListScratchpad:
    """The in-memory panel reads the agent namespace, not the store."""

    def test_returns_agent_working_files(self):
        cp = MockCheckpointer()
        cp.set_agent_files(
            "session_1",
            {
                "/scratchpad/notes.md": "# recon notes",
                "/scratchpad/outline.md": "## sections",
            },
        )

        scratchpad = _service(checkpointer=cp).list_scratchpad()

        assert scratchpad == {
            "/scratchpad/notes.md": "# recon notes",
            "/scratchpad/outline.md": "## sections",
        }

    def test_filters_out_files_outside_the_scratchpad(self):
        cp = MockCheckpointer()
        cp.set_agent_files(
            "session_1",
            {
                "/scratchpad/keep.md": "keep",
                "/elsewhere/drop.md": "drop",
            },
        )

        assert list(_service(checkpointer=cp).list_scratchpad()) == [
            "/scratchpad/keep.md"
        ]

    def test_empty_before_the_first_agent_run(self):
        assert _service().list_scratchpad() == {}

    def test_is_scoped_to_the_current_thread(self):
        cp = MockCheckpointer()
        cp.set_agent_files("session_1", {"/scratchpad/a.md": "one"})
        cp.set_agent_files("session_2", {"/scratchpad/b.md": "two"})

        assert list(_service("session_2", checkpointer=cp).list_scratchpad()) == [
            "/scratchpad/b.md"
        ]

    def test_prefix_filter_is_slash_insensitive(self):
        cp = MockCheckpointer()
        cp.set_agent_files("session_1", {"scratchpad/a.md": "no leading slash"})

        assert _service(checkpointer=cp).list_scratchpad() == {
            "scratchpad/a.md": "no leading slash"
        }


class TestPrefixStripping:
    """Only a leading prefix is stripped, never text in the middle of a name."""

    def test_strips_the_leading_findings_prefix(self):
        store = MockFileStore()
        store.write_file("findings/nmap.txt", b"x")

        assert _service(file_store=store).list_findings() == ["nmap.txt"]

    def test_does_not_strip_prefix_appearing_mid_name(self):
        store = MockFileStore()
        store.write_file("findings/findings_summary.txt", b"x")

        assert _service(file_store=store).list_findings() == [
            "findings_summary.txt"
        ]

    def test_does_not_strip_report_prefix_mid_name(self):
        store = MockFileStore()
        store.write_file("reports/reports_final.md", b"x")

        assert _service(file_store=store).list_reports() == ["reports_final.md"]

    def test_strips_leading_report_prefix(self):
        store = MockFileStore()
        store.write_file("reports/final.md", b"x")

        assert _service(file_store=store).list_reports() == ["final.md"]


class TestGrouping:
    """Display grouping keeps nested paths readable."""

    def test_groups_by_directory(self):
        grouped = _group_by_directory(
            [
                "/context/findings/a.txt",
                "/context/findings/b.txt",
                "/context/reports/c.md",
            ]
        )

        assert grouped == {
            "findings": ["a.txt", "b.txt"],
            "reports": ["c.md"],
        }

    def test_nested_paths_keep_their_remaining_shape(self):
        grouped = _group_by_directory(["/context/findings/host1/a.txt"])

        assert grouped == {"findings/host1": ["a.txt"]}

    def test_root_level_file_gets_its_own_group(self):
        grouped = _group_by_directory(["/context/loose.md"])

        assert grouped == {"(root)": ["loose.md"]}

    def test_paths_without_the_context_root_are_still_grouped(self):
        grouped = _group_by_directory(["findings/a.txt"])

        assert grouped == {"findings": ["a.txt"]}