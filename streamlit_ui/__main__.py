"""Module entry point: ``python -m streamlit_ui [streamlit args...]``.

Prints the runtime connectivity report (checkpointer / file store / llm api /
agent / observability) *before* the Streamlit server starts, then delegates to
Streamlit's own CLI. Drop-in replacement for::

    python -m streamlit run streamlit_ui/app.py

The report is NOT printed on a normal launch - only when debug is enabled::

    python -m streamlit_ui --debug                   # debug flag + default app
    python -m streamlit_ui --debug run streamlit_ui/app.py
    set STREAMLIT_RUNTIME_DEBUG=1 && python -m streamlit_ui   # or env var

See ``streamlit_ui/runtime_status.py`` for the printed lines.
"""

import sys

from streamlit.web import cli as stcli

from streamlit_ui.runtime_status import is_debug_mode, print_status_banner

_DEFAULT_ARGS = ["run", "streamlit_ui/app.py"]
_DEBUG_FLAG = "--debug"


def main(argv=None) -> None:
    """Print the debug connectivity report (when enabled), then run Streamlit.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``). Arrowed
              through unchanged, so ``python -m streamlit_ui run app.py`` works
              exactly like ``python -m streamlit run app.py``. With no
              arguments it defaults to ``run streamlit_ui/app.py``. A leading
              ``--debug`` enables the status report and is stripped before
              delegation.
    """
    args = list(argv) if argv is not None else sys.argv[1:]

    debug = is_debug_mode()
    if _DEBUG_FLAG in args:
        args.remove(_DEBUG_FLAG)
        debug = True

    if not args:
        args = _DEFAULT_ARGS

    if debug:
        print_status_banner()

    # streamlit reads sys.argv[0] only to decide its program name.
    sys.argv = ["streamlit", *args]
    stcli.main()


if __name__ == "__main__":
    main()