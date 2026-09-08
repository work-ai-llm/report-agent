"""Tests for the pre-launch runtime connectivity report (runtime_status.py).

The report shows the five components the agent needs (checkpointer, virtual
filesystem store, llm api, agent object, observability/langfuse) and whether
each is connected. Printed only when debug is enabled.
"""

import pytest
from streamlit_ui import runtime_status as rs

ALL_COMPONENTS = [
    "checkpointer",
    "virtual filesystem store",
    "llm api",
    "agent object",
    "observability (langfuse)",
]


class TestModeDetection:
    """Mock vs production and debug flag detection."""

    def test_mock_mode_detection(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        assert rs.is_mock_mode() is True

    def test_mock_mode_detection_case_insensitive_true(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "TRUE")
        assert rs.is_mock_mode() is True

    def test_production_mode_detection_when_unset(self, monkeypatch):
        monkeypatch.delenv("STREAMLIT_USE_MOCKS", raising=False)
        assert rs.is_mock_mode() is False

    def test_debug_disabled_by_default(self, monkeypatch):
        monkeypatch.delenv("STREAMLIT_RUNTIME_DEBUG", raising=False)
        assert rs.is_debug_mode() is False

    def test_debug_enabled_by_env(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_RUNTIME_DEBUG", "1")
        assert rs.is_debug_mode() is True


class TestMockModeReport:
    """Connectivity lines when STREAMLIT_USE_MOCKS=1."""

    def test_reports_five_components(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        lines = rs.component_status_lines()
        assert len(lines) == 5
        for component in ALL_COMPONENTS:
            assert any(l.startswith(component + ":") for l in lines)

    def test_checkpointer_and_filesystem_connected(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        lines = "\n".join(rs.component_status_lines())
        assert "checkpointer: connected (mock, in-memory)" in lines
        assert "virtual filesystem store: connected (mock, in-memory)" in lines

    def test_agent_connected_mock_runner(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        lines = "\n".join(rs.component_status_lines())
        assert "agent object: connected (mock agent runner)" in lines

    def test_llm_api_and_observability_not_connected(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        lines = "\n".join(rs.component_status_lines())
        assert "llm api: not connected" in lines
        assert "observability (langfuse): not connected" in lines

    def test_print_banner_is_plain_ascii(self, monkeypatch, capsys):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        rs.print_status_banner()
        out = capsys.readouterr().out
        # Plain, one-component-per-line output.
        assert out.count("\n") == 5
        assert "checkpointer: connected" in out
        assert "observability (langfuse): not connected" in out


class TestProductionModeReport:
    """Connectivity lines when the production backend is requested."""

    def test_all_components_report_not_wired(self, monkeypatch):
        monkeypatch.delenv("STREAMLIT_USE_MOCKS", raising=False)
        lines = rs.component_status_lines()
        assert len(lines) == 5
        for line in lines:
            assert line.endswith("not connected (production wiring not implemented yet "
                                  "(run with STREAMLIT_USE_MOCKS=1))")