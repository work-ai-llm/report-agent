"""Tests for the ``python -m streamlit_ui`` CLI runtime-selection flags.

The CLI translates ``--backend`` / ``--agent`` / ``--llm-backend`` /
``--streaming`` into the same environment variables the service layer already
reads, so the app, debug banner and backend construction logic are unchanged.
These tests lock the translation table: flag → env var, value validation, and
pass-through of Streamlit's own args.
"""

import os

import pytest

from streamlit_ui import __main__ as m
from streamlit_ui.services.real_backend import is_streaming_enabled

RELEVANT_KEYS = (
    "REPORT_AGENT_FORCE_SQLITE",
    "STREAMLIT_USE_REAL_AGENT",
    "STREAMLIT_USE_REAL_BACKEND",
    "STREAMLIT_USE_MOCKS",
    "STREAMLIT_STREAM",
    "LLM_BACKEND",
)
_TRUE = ("1", "true", "yes")


def _env_export() -> dict:
    """Snapshot of the env keys the CLI controls."""
    return {k: v for k, v in os.environ.items() if k in RELEVANT_KEYS}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Start every test with a clean slate for the CLI-controlled vars."""
    for key in RELEVANT_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestRuntimeFlagTranslation:
    """Flag combos and the env vars they imply."""

    def test_real_agent_plus_sqlite_with_debug(self, monkeypatch):
        monkeypatch.delenv("STREAMLIT_RUNTIME_DEBUG", raising=False)
        remaining, debug = m._apply_runtime_flags(
            ["--debug", "--backend", "sqlite", "--agent", "real"]
        )
        assert debug is True
        assert remaining == []
        assert _env_export() == {
            "REPORT_AGENT_FORCE_SQLITE": "1",
            "STREAMLIT_USE_REAL_AGENT": "1",
        }

    def test_mock_agent_plus_postgres_equals_form(self):
        remaining, debug = m._apply_runtime_flags(
            ["--agent=mock", "--backend=postgres"]
        )
        assert debug is False
        assert remaining == []
        assert _env_export() == {"STREAMLIT_USE_REAL_BACKEND": "1"}

    def test_mock_agent_plus_sqlite(self):
        m._apply_runtime_flags(["--agent", "mock", "--backend", "sqlite"])
        assert _env_export() == {
            "REPORT_AGENT_FORCE_SQLITE": "1",
            "STREAMLIT_USE_REAL_BACKEND": "1",
        }

    def test_mocks_agent_full_sandbox(self, monkeypatch):
        # --agent mocks = full in-memory sandbox, real modes cleared.
        monkeypatch.setenv("STREAMLIT_USE_REAL_AGENT", "1")
        m._apply_runtime_flags(["--agent", "mocks"])
        assert _env_export() == {"STREAMLIT_USE_MOCKS": "1"}

    def test_mocks_agent_backend_flag_still_translates(self):
        # --backend stays a pure env translation; mocks mode just ignores it.
        m._apply_runtime_flags(["--agent", "mocks", "--backend", "sqlite"])
        assert _env_export() == {
            "REPORT_AGENT_FORCE_SQLITE": "1",
            "STREAMLIT_USE_MOCKS": "1",
        }

    def test_explicit_flag_wins_over_preset_conflict(self, monkeypatch):
        # Pre-existing MOCKS/REAL_BACKEND vars must not survive --agent real.
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        monkeypatch.setenv("STREAMLIT_USE_REAL_BACKEND", "1")
        m._apply_runtime_flags(["--agent", "real"])
        assert _env_export() == {"STREAMLIT_USE_REAL_AGENT": "1"}

    def test_postgres_flag_clears_force_sqlite(self, monkeypatch):
        monkeypatch.setenv("REPORT_AGENT_FORCE_SQLITE", "1")
        m._apply_runtime_flags(["--backend", "postgres"])
        assert _env_export() == {}


class TestStreamingFlag:
    """--streaming / --no-streaming toggle the STREAMLIT_STREAM env."""

    def test_streaming_flag_enables(self):
        m._apply_runtime_flags(["--streaming"])
        assert _env_export() == {"STREAMLIT_STREAM": "1"}

    def test_no_streaming_flag_disables(self):
        m._apply_runtime_flags(["--no-streaming"])
        assert _env_export() == {"STREAMLIT_STREAM": "0"}

    def test_last_streaming_flag_wins(self):
        m._apply_runtime_flags(["--streaming", "--no-streaming"])
        assert _env_export() == {"STREAMLIT_STREAM": "0"}
        m._apply_runtime_flags(["--no-streaming", "--streaming"])
        assert _env_export() == {"STREAMLIT_STREAM": "1"}

    def test_no_streaming_flag_not_left_in_args(self):
        remaining, _ = m._apply_runtime_flags(["run", "streamlit_ui/app.py", "--no-streaming"])
        assert remaining == ["run", "streamlit_ui/app.py"]


class TestStreamingEnabledConsumer:
    """The app-side consumer honours STREAMLIT_STREAM and defaults to on."""

    def test_defaults_to_on(self, monkeypatch):
        monkeypatch.delenv("STREAMLIT_STREAM", raising=False)
        assert is_streaming_enabled() is True

    def test_truthy_variants(self, monkeypatch):
        for value in _TRUE:
            monkeypatch.setenv("STREAMLIT_STREAM", value)
            assert is_streaming_enabled() is True

    def test_disabled_by_falsy_values(self, monkeypatch):
        for value in ("0", "false", "no"):
            monkeypatch.setenv("STREAMLIT_STREAM", value)
            assert is_streaming_enabled() is False


class TestArgPassThrough:
    """Non-runtime args are untouched and the defaults still apply."""

    def test_streamlit_args_pass_through(self, monkeypatch):
        monkeypatch.setenv("STREAMLIT_USE_MOCKS", "1")
        remaining, debug = m._apply_runtime_flags(["--server.port", "8501"])
        assert remaining == ["--server.port", "8501"]
        assert debug is False
        assert _env_export() == {"STREAMLIT_USE_MOCKS": "1"}


class TestValidation:
    """Unknown values and missing values are rejected."""

    @pytest.mark.parametrize("argv", [["--backend", "mongodb"], ["--agent=marvin"]])
    def test_unknown_value_raises(self, argv):
        with pytest.raises(ValueError):
            m._apply_runtime_flags(argv)

    def test_missing_value_raises(self):
        with pytest.raises(ValueError):
            m._apply_runtime_flags(["--agent"])

    def test_main_exits_with_usage_on_error(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            m.main(["--backend", "mongodb"])
        assert excinfo.value.code == 2
        err = capsys.readouterr().err
        assert "usage:" in err
        assert "--backend" in err


class TestLlmBackendFlag:
    """``--llm-backend`` selects the chat backend and nothing else."""

    @pytest.mark.parametrize("name", ["openai_local", "openrouter", "mistral"])
    def test_each_backend_is_accepted(self, name):
        remaining, _debug = m._apply_runtime_flags(["--llm-backend", name])
        assert os.environ["LLM_BACKEND"] == name
        assert remaining == []

    def test_equals_form(self):
        remaining, _debug = m._apply_runtime_flags(["--llm-backend=openrouter"])
        assert os.environ["LLM_BACKEND"] == "openrouter"
        assert remaining == []

    def test_composes_with_other_flags(self):
        remaining, debug = m._apply_runtime_flags([
            "--debug", "--backend", "sqlite", "--agent", "real",
            "--llm-backend", "openrouter",
        ])
        assert debug is True
        assert remaining == []
        assert _env_export() == {
            "REPORT_AGENT_FORCE_SQLITE": "1",
            "STREAMLIT_USE_REAL_AGENT": "1",
            "LLM_BACKEND": "openrouter",
        }

    def test_unknown_backend_raises(self):
        with pytest.raises(ValueError, match="llm-backend"):
            m._apply_runtime_flags(["--llm-backend", "gemini"])

    def test_missing_value_raises(self):
        with pytest.raises(ValueError, match="llm-backend"):
            m._apply_runtime_flags(["--llm-backend"])

    def test_absent_flag_leaves_env_untouched(self):
        os.environ["LLM_BACKEND"] = "mistral"
        m._apply_runtime_flags(["--agent", "real"])
        assert os.environ["LLM_BACKEND"] == "mistral"

    def test_flag_overrides_dotenv_value(self, monkeypatch, capsys):
        # load_dotenv does not override, and the flag is written before the
        # agent module is imported, so the flag is what _init_llm will read.
        monkeypatch.setenv("LLM_BACKEND", "openai_local")
        m._apply_runtime_flags(["--llm-backend", "openrouter"])
        assert os.environ["LLM_BACKEND"] == "openrouter"
        out = capsys.readouterr().out
        assert "openai_local -> openrouter" in out

    def test_no_note_when_value_is_unchanged(self, monkeypatch, capsys):
        monkeypatch.setenv("LLM_BACKEND", "openrouter")
        m._apply_runtime_flags(["--llm-backend", "openrouter"])
        assert "->" not in capsys.readouterr().out

    def test_model_vars_are_never_written(self, monkeypatch):
        # The flag selects a backend, never a model. AGENT_MODEL /
        # AGENT_EMULATOR_MODEL are backend-agnostic, so silently rewriting them
        # here would be choosing a model on the user's behalf; the runtime warns
        # instead when an override cannot belong to the selected backend.
        monkeypatch.setenv("AGENT_MODEL", "ornith")
        monkeypatch.setenv("AGENT_EMULATOR_MODEL", "ornith")
        m._apply_runtime_flags(["--llm-backend", "openrouter"])
        assert os.environ["AGENT_MODEL"] == "ornith"
        assert os.environ["AGENT_EMULATOR_MODEL"] == "ornith"

    def test_accepted_values_match_the_agent_table(self):
        # __main__ must not import agent.report_agent (that would drag in the
        # runtime and its dependencies), so the two lists are duplicated. This
        # is what keeps them from drifting apart.
        from agent.report_agent import _BACKEND_MODEL_DEFAULTS

        assert set(m._LLM_BACKENDS) == set(_BACKEND_MODEL_DEFAULTS)

    def test_help_documents_the_flag_and_the_model_limit(self, capsys):
        with pytest.raises(SystemExit):
            m.main(["--help"])
        out = capsys.readouterr().out
        assert "--llm-backend" in out
        assert "openai_local" in out
        # The help has to say where model ids actually come from, since the flag
        # cannot set them.
        assert "AGENT_MODEL" in out
        assert "_BACKEND_MODEL_DEFAULTS" in out


class TestHelp:
    """-h / --help print the custom CLI help and exit 0 (not streamlit's)."""

    @pytest.mark.parametrize("flag", ["-h", "--help"])
    def test_help_exits_zero_with_usage(self, flag, capsys):
        with pytest.raises(SystemExit) as excinfo:
            m.main([flag])
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "usage:" in out
        assert "--agent" in out
        assert "mocks" in out

    def test_help_wins_even_with_other_flags(self, capsys):
        with pytest.raises(SystemExit) as excinfo:
            m.main(["--agent", "real", "--help"])
        assert excinfo.value.code == 0