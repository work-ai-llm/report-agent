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
        # Five connectivity components, plus the streaming mode line - which is a
        # mode rather than a connection, but is reported here so --no-streaming
        # is visible without guessing.
        assert len(lines) == 6
        for component in ALL_COMPONENTS:
            assert any(l.startswith(component + ":") for l in lines)

    def test_report_names_the_streaming_mode(self, monkeypatch):
        """--no-streaming is silent behaviour, so the banner has to state it."""
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        monkeypatch.delenv("STREAMLIT_STREAM", raising=False)
        assert any("token streaming" in l for l in rs.component_status_lines())

        monkeypatch.setenv("STREAMLIT_STREAM", "0")
        assert any("no streaming" in l for l in rs.component_status_lines())

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
        # Plain, one-component-per-line output (five components + streaming mode).
        assert out.count("\n") == 6
        assert "checkpointer: connected" in out
        assert "observability (langfuse): not connected" in out
        assert "streaming:" in out


class TestProductionModeReport:
    """Connectivity lines when no real runtime is available to probe."""

    def test_all_components_report_not_probed(self, monkeypatch):
        # No real runtime means nothing was checked, so nothing may claim to be
        # "connected". This replaced a branch that returned five hardcoded
        # "connected (production ...)" lines - including one naming
        # MISTRAL_API_KEY while the usual backend is a local llama.cpp server.
        # An unverified green banner is worse than an honest gap: it survives a
        # broken setup instead of pointing at it.
        monkeypatch.delenv("STREAMLIT_USE_MOCKS", raising=False)
        monkeypatch.delenv("STREAMLIT_USE_REAL_BACKEND", raising=False)
        monkeypatch.delenv("STREAMLIT_USE_REAL_AGENT", raising=False)
        lines = rs.component_status_lines()
        assert len(lines) == 5
        for line in lines:
            assert line.startswith(("checkpointer:", "virtual filesystem store:",
                                    "llm api:", "agent object:", "observability (langfuse):"))
            assert "connected" not in line
            assert "not probed" in line

    def test_llm_line_explains_how_to_probe(self, monkeypatch):
        monkeypatch.delenv("STREAMLIT_USE_MOCKS", raising=False)
        monkeypatch.delenv("STREAMLIT_USE_REAL_BACKEND", raising=False)
        monkeypatch.delenv("STREAMLIT_USE_REAL_AGENT", raising=False)
        llm_line = next(ln for ln in rs.component_status_lines()
                        if ln.startswith("llm api:"))
        assert "--agent real" in llm_line

    def test_real_agent_line_names_the_resolved_backend(self, monkeypatch):
        # The point of storing runtime.llm_backend: with the real agent up, the
        # line has to say WHICH backend answered, because the backend decides
        # which model ids are valid. A stale AGENT_MODEL carried across backends
        # only fails later, at request time, with a 404 - so this line is the
        # only place that can be checked cheaply.
        monkeypatch.delenv("STREAMLIT_USE_MOCKS", raising=False)
        monkeypatch.setenv("STREAMLIT_USE_REAL_AGENT", "1")
        llm_line = next(ln for ln in rs.component_status_lines()
                        if ln.startswith("llm api:"))
        assert "backend:" in llm_line
        assert "model:" in llm_line