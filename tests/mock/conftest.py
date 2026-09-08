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
