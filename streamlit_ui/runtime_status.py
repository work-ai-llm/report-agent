"""Runtime status banner - shows which backend pieces are connected.

Printed to the terminal *before* the Streamlit server starts (via the
``python -m streamlit_ui`` launcher in ``streamlit_ui/__main__.py``) so you can
see at a glance which "insides" the app will use:

- mock mode (``STREAMLIT_USE_MOCKS=1``): in-memory checkpointer / file store /
  mock agent runner.
- production mode: the real backend (SQLite/Postgres + live agent). NOT wired
  yet - ``_build_production_service`` raises ``NotImplementedError``.

```text
--------------------------------------------------------------------
  Pen-Test Reporting Agent - runtime status (mock mode)
    [ok] mock checkpointer is connected      (in-memory session store)
    [ok] mock filesystem store is connected  (in-memory findings/reports)
    [ok] mock agent is connected             (scripted streaming runner, no LLM)
--------------------------------------------------------------------
```

The status probe deliberately uses the same dependency objects the app will
use. For mock mode the objects are stateless in-memory singletons, so a fresh
probe and the app's ``st.cache_resource`` singleton are equivalent. When the
real backend lands, this is where the SQLite/Postgres/agent connectivity checks
should be added.
"""

import os
from typing import List

_MOCK_MODES = ("1", "true", "yes")


def is_mock_mode() -> bool:
    """Return True when the app will run in mock mode."""
    return os.environ.get("STREAMLIT_USE_MOCKS", "").strip().lower() in _MOCK_MODES


def status_lines() -> List[str]:
    """Return the 'what is connected' lines for the current run mode.

    Returns:
        List of banner lines (without decorations). Each line describes one
        backend piece and whether it is connected/available.
    """
    if is_mock_mode():
        return _mock_status_lines()
    return [
        "[!] production backend is NOT wired yet",
        "    real sqlite/postgres + live agent adapters are pending "
        "(_build_production_service raises NotImplementedError)",
        "    start with STREAMLIT_USE_MOCKS=1 to run in mock mode",
    ]


def _mock_status_lines() -> List[str]:
    lines: List[str] = []
    from tests.mock.mock_objects_streamlit import (
        MockCheckpointer,
        MockFileStore,
        MockAgentRunner,
    )

    try:
        MockCheckpointer().list_threads()
        lines.append("[ok] mock checkpointer is connected      (in-memory session store)")
    except Exception as exc:  # pragma: no cover - defensive
        lines.append(f"[!!] mock checkpointer FAILED: {exc}")

    try:
        store = MockFileStore()
        store.list_files()
        lines.append("[ok] mock filesystem store is connected  (in-memory findings/reports)")
    except Exception as exc:  # pragma: no cover - defensive
        lines.append(f"[!!] mock filesystem store FAILED: {exc}")

    try:
        runner = MockAgentRunner()
        list(runner.run("ping", "status_probe"))
        lines.append("[ok] mock agent is connected             (scripted streaming runner, no LLM)")
    except Exception as exc:  # pragma: no cover - defensive
        lines.append(f"[!!] mock agent FAILED: {exc}")

    return lines


def print_status_banner() -> None:
    """Print the runtime status banner to stdout (ASCII-only, console-safe)."""
    mode = "mock" if is_mock_mode() else "production"
    width = 68
    print("-" * width)
    print(f"  Pen-Test Reporting Agent - runtime status ({mode} mode)")
    for line in status_lines():
        print("    " + line)
    print("-" * width)