"""Tests for the two sidebar filesystem panels: the store-backed filesystem
view and the agent's scratchpad view.

The two read different things. ``list_filesystem`` goes through the file
store, which holds everything routed through ``/context/``. ``list_scratchpad``
does not read storage at all: the agent's scratchpad is held by an in-memory
backend that is never checkpointed, so the panel replays the session's own
persisted tool calls instead. That is why it survives a reload.

## What these tests verify

- The filesystem view lists the whole store, not a hand-picked slice, so the
  context library shows up alongside findings and reports.
- Display paths carry the ``/context/`` root the agent itself uses, even though
  the store strips it before writing.
- The scratchpad is rebuilt from ``write_file`` / ``edit_file`` tool calls, in
  order, so the newest content wins for a path that was written twice.
- Tool calls outside the scratchpad are ignored, and a substitution-style edit
  that cannot be replayed is dropped rather than shown as wrong content.
- Only a leading prefix is stripped when listing findings and reports. The
  previous ``str.replace`` also rewrote the same text mid-name, so
  ``findings/findings_summary.txt`` displayed as ``summary.txt`` and a later
  write using that displayed name targeted a different key.
- Directory grouping for display keeps nested paths readable.

## Running

```bash
python -m pytest tests/mock/test_filesystem_views.py -v
```
"""

from typing import Any, Dict, List

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


def _write(path: str, content: str) -> Dict[str, Any]:
    """A persisted ``write_file`` history entry.

    Persisted entries are FLAT - ``name``/``args`` sit at the top level. The
    live event carries the same fields nested under ``data``; see
    ``test_accepts_the_nested_live_event_shape``.
    """
    return {
        "type": "tool_call",
        "name": "write_file",
        "args": {"file_path": path, "content": content},
        "result": "ok",
    }


def _edit(path: str, **args: Any) -> Dict[str, Any]:
    return {"type": "tool_call", "name": "edit_file", "args": {"file_path": path, **args}}


def _service_with_history(history: List[Dict[str, Any]]) -> PenTestAgentService:
    cp = MockCheckpointer()
    cp.put("session_1", {"messages": history})
    return _service(checkpointer=cp)


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
    """The scratchpad is rebuilt from the session's persisted tool calls."""

    def test_reconstructs_files_from_write_file_calls(self):
        service = _service_with_history(
            [
                _write("/scratchpad/1_host_inventory.md", "# Host Inventory"),
                _write("/scratchpad/2_ad_infrastructure.md", "# AD"),
            ]
        )

        assert service.list_scratchpad() == {
            "/scratchpad/1_host_inventory.md": "# Host Inventory",
            "/scratchpad/2_ad_infrastructure.md": "# AD",
        }

    def test_empty_before_the_agent_wrote_anything(self):
        assert _service_with_history([]).list_scratchpad() == {}

    def test_ignores_tool_calls_outside_the_scratchpad(self):
        service = _service_with_history(
            [
                _write("/context/reports/final.md", "a report"),
                _write("/elsewhere/notes.md", "not scratchpad"),
            ]
        )

        assert service.list_scratchpad() == {}

    def test_ignores_non_write_tools(self):
        service = _service_with_history(
            [
                {
                    "type": "tool_call",
                    "name": "read_file",
                    "args": {"file_path": "/scratchpad/x.md"},
                },
                {"type": "message", "data": "done"},
                {"role": "user", "content": "go"},
            ]
        )

        assert service.list_scratchpad() == {}

    def test_latest_write_to_a_path_wins(self):
        service = _service_with_history(
            [
                _write("/scratchpad/draft.md", "first"),
                _write("/scratchpad/draft.md", "second"),
            ]
        )

        assert service.list_scratchpad() == {"/scratchpad/draft.md": "second"}

    def test_edit_with_full_body_replaces_content(self):
        service = _service_with_history(
            [
                _write("/scratchpad/draft.md", "old"),
                _edit("/scratchpad/draft.md", content="new"),
            ]
        )

        assert service.list_scratchpad() == {"/scratchpad/draft.md": "new"}

    def test_edit_with_substitution_is_replayed_against_known_content(self):
        service = _service_with_history(
            [
                _write("/scratchpad/draft.md", "alpha beta"),
                _edit("/scratchpad/draft.md", old_string="beta", new_string="gamma"),
            ]
        )

        assert service.list_scratchpad() == {"/scratchpad/draft.md": "alpha gamma"}

    def test_unreplayable_substitution_is_dropped_not_faked(self):
        """An edit with no known prior content must not invent a result."""
        service = _service_with_history(
            [_edit("/scratchpad/draft.md", old_string="beta", new_string="gamma")]
        )

        assert service.list_scratchpad() == {}

    def test_is_scoped_to_the_current_thread(self):
        cp = MockCheckpointer()
        cp.put("session_1", {"messages": [_write("/scratchpad/a.md", "one")]})
        cp.put("session_2", {"messages": [_write("/scratchpad/b.md", "two")]})

        service = PenTestAgentService(
            thread_id="session_2",
            checkpointer=cp,
            file_store=MockFileStore(),
            agent_runner=MockAgentRunner(),
        )

        assert service.list_scratchpad() == {"/scratchpad/b.md": "two"}

    def test_survives_a_service_rebuild_from_the_same_history(self):
        """A reload creates a new service over the same persisted history."""
        cp = MockCheckpointer()
        cp.put("session_1", {"messages": [_write("/scratchpad/notes.md", "# notes")]})

        first = _service(checkpointer=cp).list_scratchpad()
        second = _service(checkpointer=cp).list_scratchpad()

        assert first == second == {"/scratchpad/notes.md": "# notes"}

    def test_tolerates_malformed_entries(self):
        service = _service_with_history(
            [
                None,
                "a bare string",
                {"type": "tool_call"},
                {"type": "tool_call", "data": None},
                {"type": "tool_call", "name": "write_file", "args": None},
                {"type": "tool_call", "name": "write_file", "args": {}},
            ]
        )

        assert service.list_scratchpad() == {}

    def test_accepts_the_nested_live_event_shape(self):
        """The live stream nests name/args under data; persisted history is flat.

        Reading only one of the two shapes is what made this panel render empty
        against real data, so both are pinned here.
        """
        service = _service_with_history(
            [
                {
                    "type": "tool_call",
                    "data": {
                        "name": "write_file",
                        "args": {"file_path": "/scratchpad/n.md", "content": "nested"},
                    },
                }
            ]
        )

        assert service.list_scratchpad() == {"/scratchpad/n.md": "nested"}


class TestPrefixStripping:
    """Only a leading prefix is stripped, never text in the middle of a name."""

    def test_strips_the_leading_findings_prefix(self):
        store = MockFileStore()
        store.write_file("findings/nmap.txt", b"x")

        assert _service(file_store=store).list_findings() == ["nmap.txt"]

    def test_does_not_strip_prefix_appearing_mid_name(self):
        store = MockFileStore()
        store.write_file("findings/findings_summary.txt", b"x")

        assert _service(file_store=store).list_findings() == ["findings_summary.txt"]

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

        assert grouped == {"findings": ["a.txt", "b.txt"], "reports": ["c.md"]}

    def test_nested_paths_keep_their_remaining_shape(self):
        grouped = _group_by_directory(["/context/findings/host1/a.txt"])

        assert grouped == {"findings/host1": ["a.txt"]}

    def test_root_level_file_gets_its_own_group(self):
        grouped = _group_by_directory(["/context/loose.md"])

        assert grouped == {"(root)": ["loose.md"]}

    def test_paths_without_the_context_root_are_still_grouped(self):
        grouped = _group_by_directory(["findings/a.txt"])

        assert grouped == {"findings": ["a.txt"]}