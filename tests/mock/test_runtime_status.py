"""Tests for the pre-launch runtime status banner (streamlit_ui/runtime_status.py).

The banner tells the user which backend pieces will be used (mock checkpointer,
mock file store, mock agent) before the Streamlit server starts.
"""

import pytest
from streamlit_ui import runtime_status as rs


class TestMockModeBanner:
    """Status lines when STREAMLIT_USE_MOCKS=1."""

    def test_mock_mode_detection(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        assert rs.is_mock_mode() is True

    def test_mock_mode_detection_case_insensitive_true(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "TRUE")
        assert rs.is_mock_mode() is True

    def test_production_mode_detection_when_unset(self, monkeypatch):
        monkeypatch.delenv("STREAMLIT_USE_MOCKS", raising=False)
        assert rs.is_mock_mode() is False

    def test_mock_lines_report_all_pieces_connected(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        lines = rs.status_lines()
        assert len(lines) == 3
        assert any("mock checkpointer is connected" in l for l in lines)
        assert any("mock filesystem store is connected" in l for l in lines)
        assert any("mock agent is connected" in l for l in lines)

    def test_print_banner_renders_without_error(self, monkeypatch, capsys):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        rs.print_status_banner()
        out = capsys.readouterr().out
        assert "runtime status (mock mode)" in out
        assert "mock checkpointer is connected" in out
        assert "mock agent is connected" in out


class TestProductionModeBanner:
    """Status lines when the production backend is requested."""

    def test_production_lines_report_not_wired(self, monkeypatch):
        monkeypatch.delenv("STREAMLIT_USE_MOCKS", raising=False)
        lines = rs.status_lines()
        assert any("production backend is NOT wired yet" in l for l in lines)
        assert any("STREAMLIT_USE_MOCKS=1" in l for l in lines)