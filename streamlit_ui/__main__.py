"""Module entry point: ``python -m streamlit_ui [streamlit args...]``.

Prints the runtime status banner *before* the Streamlit server starts, then
delegates to Streamlit's own CLI. This is the drop-in replacement for::

    python -m streamlit run streamlit_ui/app.py

Example::

    python -m streamlit_ui run streamlit_ui/app.py

The banner ("mock checkpointer is connected", ...) is visible in the terminal
right above Streamlit's "Uvicorn server started" line - see
``streamlit_ui/runtime_status.py``.
"""

import sys

from streamlit.web import cli as stcli

from streamlit_ui.runtime_status import print_status_banner


def main(argv=None) -> None:
    """Print the status banner, then run the normal Streamlit CLI.

    Args:
        argv: Optional argument list (defaults to ``sys.argv[1:]``). Arrowed
              through unchanged, so ``python -m streamlit_ui run app.py`` works
              exactly like ``python -m streamlit run app.py``.
    """
    print_status_banner()
    args = list(argv) if argv is not None else sys.argv[1:]
    # streamlit reads sys.argv[0] only to decide its program name.
    sys.argv = ["streamlit", *args]
    stcli.main()


if __name__ == "__main__":
    main()