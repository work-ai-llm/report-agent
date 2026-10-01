"""Module entry point: ``python -m streamlit_ui [options] [streamlit args...]``.

Prints the runtime connectivity report (checkpointer / file store / llm api /
agent / observability) *before* the Streamlit server starts, then delegates to
Streamlit's own CLI. Drop-in replacement for::

    python -m streamlit run streamlit_ui/app.py

Runtime selection is done with two CLI flags (instead of environment
variables - those still work when a flag is omitted):

- ``--backend sqlite|postgres`` - storage backend for the agent's persistence
  layer. ``sqlite`` forces the local SQLite store (``agent/agent_session.db``);
  ``postgres`` uses Postgres when ``POSTGRES_URI`` is reachable and falls back
  to SQLite otherwise. Default: ``postgres``.
- ``--agent real|mock|mocks`` - agent runner. ``real`` runs the live LangGraph
  agent (needs model keys in ``.env``); ``mock`` uses the mock agent against
  real persistence (no API keys needed; sessions/files still survive reloads);
  ``mocks`` is the full in-memory sandbox (mock checkpointer, store AND agent).
  Default: ``real``.
- ``--streaming`` / ``--no-streaming`` - render live token deltas in the chat
  (default: streaming on). **Work in progress** - the flag is plumbed through
  to ``STREAMLIT_STREAM`` but live token streaming is not wired up yet; the
  chat currently shows tool calls live and a single aggregated final answer.

Examples::

    python -m streamlit_ui                               # production (real agent, postgres)
    python -m streamlit_ui --backend sqlite --agent real --debug
    python -m streamlit_ui --agent mock --backend sqlite run streamlit_ui/app.py
    python -m streamlit_ui --agent mocks                 # full in-memory mock sandbox
    python -m streamlit_ui --agent real --backend sqlite --no-streaming

The selected mode is fixed for the lifetime of the server process. The report
is NOT printed on a normal launch - only when debug is enabled::

    python -m streamlit_ui --debug                       # debug flag + default app
    set STREAMLIT_RUNTIME_DEBUG=1 && python -m streamlit_ui   # or env var

See ``streamlit_ui/runtime_status.py`` for the printed lines.
"""

import os
import sys

from streamlit.web import cli as stcli

from streamlit_ui.runtime_status import is_debug_mode, print_status_banner

_DEFAULT_ARGS = ["run", "streamlit_ui/app.py"]
_DEBUG_FLAG = "--debug"
_BACKEND_FLAG = "--backend"
_AGENT_FLAG = "--agent"
_STREAMING_FLAG = "--streaming"
_NO_STREAMING_FLAG = "--no-streaming"
_BACKENDS = ("sqlite", "postgres")
_AGENTS = ("mock", "real", "mocks")
_HELP_FLAGS = ("-h", "--help")


def _usage(prog: str = "python -m streamlit_ui") -> str:
    """The custom CLI help text (shown on ``-h`` / ``--help`` / errors)."""
    return (
        f"usage: {prog} [--debug] [--backend sqlite|postgres] "
        f"[--agent mock|real|mocks] [--streaming|--no-streaming] "
        f"[streamlit args...]\n"
        f"\n"
        f"Runtime selection (each flag overrides the equivalent env var when\n"
        f"present; flags are stripped before delegating to Streamlit):\n"
        f"\n"
        f"  --debug                     print the connectivity report (same as\n"
        f"                              STREAMLIT_RUNTIME_DEBUG=1)\n"
        f"  --backend sqlite|postgres   persistence backend. sqlite forces the\n"
        f"                              local store (agent/agent_session.db);\n"
        f"                              postgres uses POSTGRES_URI and falls back\n"
        f"                              to sqlite. Default: postgres\n"
        f"  --agent mock|real|mocks     agent runner:\n"
        f"      real    live LangGraph agent (needs model keys in .env)\n"
        f"      mock    mock agent + real persistence (no API keys needed)\n"
        f"      mocks   full in-memory sandbox (mock checkpointer, store + agent)\n"
        f"                              Default: real\n"
        f"  --streaming / --no-streaming   render live token deltas in the chat\n"
        f"                              (Default: streaming on)\n"
        f"                              (WIP: flag is plumbed but live token\n"
        f"                              streaming is not implemented yet - the\n"
        f"                              chat shows tool calls + one final answer)\n"
        f"\n"
        f"If no streamlit args are given, runs: {prog} "
        f"{' '.join(_DEFAULT_ARGS)}\n"
        f"Run streamlit's own CLI help with: python -m streamlit --help\n"
    )


def _apply_runtime_flags(args):
    """Consume the runtime-selection flags and translate them to env vars.

    Args:
        args: Argument list (``argv[1:]``).

    Returns:
        Tuple ``(remaining, debug)`` where ``remaining`` is ``args`` with
        ``--debug`` / ``--backend`` / ``--agent`` stripped and ``debug`` is
        True when ``--debug`` was present.

    Raises:
        ValueError: A flag has no value or an unrecognized value.

    A present flag always wins over any pre-existing env value; a flag that is
    absent leaves the environment untouched (so the usual env-var paths and
    the production default still behave as before).
    """
    remaining: list = []
    backend = None
    agent = None
    streaming = None
    debug = False
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == _DEBUG_FLAG:
            debug = True
        elif arg == _STREAMING_FLAG or arg == _NO_STREAMING_FLAG:
            streaming = arg == _STREAMING_FLAG
        elif arg == _BACKEND_FLAG or arg == _AGENT_FLAG:
            flag = arg
            i += 1
            if i >= len(args):
                raise ValueError(f"{flag} requires a value")
            value = args[i]
            if flag == _BACKEND_FLAG:
                if value not in _BACKENDS:
                    raise ValueError(f"{flag}: unknown value {value!r}; choose from {_BACKENDS}")
                backend = value
            else:
                if value not in _AGENTS:
                    raise ValueError(f"{flag}: unknown value {value!r}; choose from {_AGENTS}")
                agent = value
        elif arg.startswith(_BACKEND_FLAG + "=") or arg.startswith(_AGENT_FLAG + "="):
            flag, value = arg.split("=", 1)
            if flag == _BACKEND_FLAG:
                if value not in _BACKENDS:
                    raise ValueError(f"{flag}: unknown value {value!r}; choose from {_BACKENDS}")
                backend = value
            else:
                if value not in _AGENTS:
                    raise ValueError(f"{flag}: unknown value {value!r}; choose from {_AGENTS}")
                agent = value
        else:
            remaining.append(arg)
        i += 1

    if backend == "sqlite":
        os.environ["REPORT_AGENT_FORCE_SQLITE"] = "1"
    elif backend == "postgres":
        os.environ.pop("REPORT_AGENT_FORCE_SQLITE", None)

    if agent == "real":
        os.environ["STREAMLIT_USE_REAL_AGENT"] = "1"
        os.environ.pop("STREAMLIT_USE_MOCKS", None)
        os.environ.pop("STREAMLIT_USE_REAL_BACKEND", None)
    elif agent == "mock":
        os.environ["STREAMLIT_USE_REAL_BACKEND"] = "1"
        os.environ.pop("STREAMLIT_USE_MOCKS", None)
        os.environ.pop("STREAMLIT_USE_REAL_AGENT", None)
    elif agent == "mocks":
        os.environ["STREAMLIT_USE_MOCKS"] = "1"
        os.environ.pop("STREAMLIT_USE_REAL_BACKEND", None)
        os.environ.pop("STREAMLIT_USE_REAL_AGENT", None)

    # NOTE(WIP): streaming is not implemented yet. This only sets
    # STREAMLIT_STREAM for the future streaming work; it is inert today.
    if streaming is not None:
        os.environ["STREAMLIT_STREAM"] = "1" if streaming else "0"

    return remaining, debug


def main(argv=None) -> None:
    """Print the debug connectivity report (when enabled), then run Streamlit.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``). Runtime
              selection flags (``--backend`` / ``--agent`` / ``--debug``) are
              consumed and stripped; everything else is passed through, so
              ``python -m streamlit_ui run app.py`` works exactly like ``python
              -m streamlit run app.py``. With no arguments it defaults to ``run
              streamlit_ui/app.py``.
    """
    args = list(argv) if argv is not None else sys.argv[1:]

    if any(flag in args for flag in _HELP_FLAGS):
        print(_usage())
        raise SystemExit(0)

    try:
        args, flag_debug = _apply_runtime_flags(args)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(_usage(), file=sys.stderr)
        raise SystemExit(2) from exc

    debug = flag_debug or is_debug_mode()

    if not args:
        args = _DEFAULT_ARGS

    if debug:
        print_status_banner()

    # streamlit reads sys.argv[0] only to decide its program name.
    sys.argv = ["streamlit", *args]
    stcli.main()


if __name__ == "__main__":
    main()