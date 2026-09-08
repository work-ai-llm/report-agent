"""Runtime connectivity debug - which components are actually connected.

Printed to the terminal *before* the Streamlit server starts when debug is
enabled (via ``python -m streamlit_ui --debug`` or ``STREAMLIT_RUNTIME_DEBUG=1``).

The report shows the five components the agent needs for work and their current
state:

1. checkpointer            - session/thread persistence
2. virtual filesystem store - findings + reports storage
3. llm api                 - the model the agent calls
4. agent object            - the LangGraph/agent runner
5. observability (langfuse) - tracing/telemetry

Each line is plain text, one per component, so at a glance you can confirm what
got gathered together correctly and what broke down:

```text
checkpointer: connected (mock, in-memory)
virtual filesystem store: connected (mock, in-memory)
llm api: not connected (mock mode has no real LLM - scripted responses)
agent object: connected (mock agent runner)
observability (langfuse): not connected (not wired yet)
```

Three modes are reported:

- ``STREAMLIT_USE_MOCKS=1`` (default for dev) - all in-memory mocks.
- ``STREAMLIT_USE_REAL_BACKEND=1`` (partial swap) - real SQLite/Postgres
  checkpointer + store, still the mock agent (no LLM wired yet).
- neither - production seam, not implemented (raises).

The probes use the same dependency objects the app will use. For mock mode the
objects are stateless in-memory singletons, so a fresh probe and the app's
``st.cache_resource`` singleton are equivalent. In real-backend mode the probe
shares the process-wide backend (``real_backend.get_real_backend()``), so it
reports exactly what the app will connect to.
"""

import os
from typing import List

_MOCK_MODES = ("1", "true", "yes")
_DEBUG_MODES = ("1", "true", "yes")


def is_mock_mode() -> bool:
    """Return True when the app will run in mock mode."""
    return os.environ.get("STREAMLIT_USE_MOCKS", "").strip().lower() in _MOCK_MODES


def is_real_backend_mode() -> bool:
    """Return True when the app will run the partial (real persistence) swap."""
    from streamlit_ui.services.real_backend import is_real_backend_mode as _real

    return _real()


def is_debug_mode() -> bool:
    """Return True when the debug status report should be printed.

    Enabled by ``STREAMLIT_RUNTIME_DEBUG=1`` (also ``true``/``yes``). The
    ``--debug`` launcher flag enables it too (see ``__main__.py``).
    """
    return os.environ.get("STREAMLIT_RUNTIME_DEBUG", "").strip().lower() in _DEBUG_MODES


def component_status_lines() -> List[str]:
    """Return one line per program component: connected or not (with detail).

    Returns:
        List of plain-text lines for the five work components (checkpointer,
        virtual filesystem store, llm api, agent object, observability).
    """
    if is_mock_mode():
        return _mock_component_lines()
    if is_real_backend_mode():
        return _real_backend_component_lines()
    return _production_component_lines()


def _mock_component_lines() -> List[str]:
    from tests.mock.mock_objects_streamlit import (
        MockCheckpointer,
        MockFileStore,
        MockAgentRunner,
    )

    lines: List[str] = []

    try:
        MockCheckpointer().list_threads()
        lines.append("checkpointer: connected (mock, in-memory)")
    except Exception as exc:  # pragma: no cover - defensive
        lines.append(f"checkpointer: not connected (raised: {exc})")

    try:
        store = MockFileStore()
        store.list_files()
        lines.append("virtual filesystem store: connected (mock, in-memory)")
    except Exception as exc:  # pragma: no cover - defensive
        lines.append(f"virtual filesystem store: not connected (raised: {exc})")

    lines.append("llm api: not connected (mock mode has no real LLM - scripted responses)")

    try:
        runner = MockAgentRunner()
        list(runner.run("ping", "status_probe"))
        lines.append("agent object: connected (mock agent runner)")
    except Exception as exc:  # pragma: no cover - defensive
        lines.append(f"agent object: not connected (raised: {exc})")

    lines.append("observability (langfuse): not connected (not wired yet)")

    return lines


def _real_backend_component_lines() -> List[str]:
    from streamlit_ui.services import real_backend as rb

    try:
        runner, runtime = rb.get_real_backend()
    except Exception as exc:  # pragma: no cover - defensive
        reason = f"initialization failed: {exc}"
        lines = [f"checkpointer: not connected ({reason})"]
        lines.append(f"virtual filesystem store: not connected ({reason})")
        lines.append(f"llm api: not connected ({reason})")
        lines.append(f"agent object: not connected ({reason})")
        lines.append(f"observability (langfuse): not connected ({reason})")
        return lines

    label = rb.backend_label(runtime)
    checkpointer = rb.RealCheckpointerAdapter(runner, runtime.checkpointer)
    store = rb.RealFileStoreAdapter(runner, runtime.store)

    lines: List[str] = []

    try:
        checkpointer.list_threads()
        lines.append(f"checkpointer: connected ({label})")
    except Exception as exc:  # pragma: no cover - defensive
        lines.append(f"checkpointer: not connected (raised: {exc})")

    try:
        store.list_files()
        lines.append(f"virtual filesystem store: connected ({label})")
    except Exception as exc:  # pragma: no cover - defensive
        lines.append(f"virtual filesystem store: not connected (raised: {exc})")

    lines.append("llm api: not connected (partial swap keeps the mock agent - no LLM wired)")

    lines.append("agent object: not connected (partial swap keeps the mock agent runner)")

    if runtime.langfuse_ok:
        lines.append("observability (langfuse): connected (langfuse handler attached)")
    else:
        lines.append("observability (langfuse): not connected (no langfuse credentials/offline)")

    return lines


def _production_component_lines() -> List[str]:
    reason = "production wiring not implemented yet (run with STREAMLIT_USE_MOCKS=1)"
    return [
        f"checkpointer: not connected ({reason})",
        f"virtual filesystem store: not connected ({reason})",
        f"llm api: not connected ({reason})",
        f"agent object: not connected ({reason})",
        f"observability (langfuse): not connected ({reason})",
    ]


def print_status_banner() -> None:
    """Print the plain five-component connectivity report to stdout."""
    for line in component_status_lines():
        print(line)