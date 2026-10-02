"""Conftest for the mock/UI-mocked test suite.

The repo ``.env`` sets ``POSTGRES_URI``. ``agent.report_agent`` snapshots it
into the module-level ``DB_URI`` at import time, so every
``create_real_backend()`` in these tests would attempt a Postgres connection
first (several seconds of failed connects each) before falling back to SQLite.

These tests are intentionally hermetic/SQLite, so neutralize the variable
before any test module can import ``report_agent``.
"""

import os

# Empty string (not delenv): `agent.report_agent` runs ``load_dotenv()`` at
# import, and dotenv only sets vars that are ABSENT from os.environ. Popping
# the var lets .env re-add it; an empty value blocks that.
os.environ["POSTGRES_URI"] = ""

# If report_agent was already imported (e.g. by another conftest), its module
# snapshot `DB_URI` still holds the real value. Neutralize it too.
try:
    from agent import report_agent as _ra

    _ra.DB_URI = None
except Exception:  # noqa: BLE001 - nothing imported yet is fine
    pass

import asyncio
import pytest
from unittest.mock import MagicMock
from agent.report_agent import runtime

mock_langfuse = MagicMock()


@pytest.fixture(autouse=True)
def patch_runtime_langfuse():
    original = runtime.langfuse
    runtime.langfuse = mock_langfuse
    yield
    runtime.langfuse = original


@pytest.fixture(autouse=True)
def isolate_global_lock():
    import agent.a2a_connector as conn

    conn.global_execution_lock = asyncio.Lock()
    yield
    try:
        if conn.global_execution_lock.locked():
            conn.global_execution_lock.release()
    except Exception:
        pass
    conn.global_execution_lock = asyncio.Lock()