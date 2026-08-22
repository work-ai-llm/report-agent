import sys
from pathlib import Path

root_dir = Path(__file__).parent.parent.parent
sys.path.insert(0, str(root_dir))


def test_report_agent_imports_without_side_effects():
    """Verify that importing report_agent does NOT trigger side effects."""
    import importlib
    import agent.report_agent as mod

    importlib.reload(mod)

    assert hasattr(mod, 'runtime'), 'runtime object not found in report_agent'
    assert type(mod.runtime).__name__ == 'AgentRuntime', 'runtime is not AgentRuntime'

    assert mod.runtime.agent is None
    assert mod.runtime.pool is None
    assert mod.runtime.checkpointer is None
    assert mod.runtime.store is None
    assert mod.runtime.langfuse is None
    assert mod.runtime.langfuse_handler is None
    assert mod.runtime.system_prompt_text is None
    assert mod.runtime.fs_middleware is None
    assert mod.runtime.llm is None
    assert mod.runtime.tool_emulator is None
    assert mod.runtime.retry_middleware is None


def test_module_level_constants_exist():
    """Verify that module-level constants are available without initialization."""
    from agent.report_agent import DB_URI, CONTEXT_DIR, sys_prompt_path

    assert isinstance(DB_URI, str), 'DB_URI should be a string'
    assert DB_URI.startswith('postgresql://'), 'DB_URI should start with postgresql://'
    assert isinstance(CONTEXT_DIR, Path), 'CONTEXT_DIR should be a Path'
    assert isinstance(sys_prompt_path, Path), 'sys_prompt_path should be Path'


def test_agent_runtime_full_initialization():
    """Full initialization test - calls runtime.initialize() and verifies each layer.

    Requires: Langfuse server, Postgres, Mistral API key.
    This test may fail if external services are unreachable.
    """
    import asyncio
    from agent.report_agent import runtime

    asyncio.get_event_loop().run_until_complete(runtime.initialize())

    # 1. Langfuse
    assert runtime.langfuse is not None, 'langfuse client not initialized'
    assert runtime.langfuse_handler is not None, 'langfuse handler not initialized'

    # 2. System prompt
    assert runtime.system_prompt_text is not None, 'system prompt not loaded'
    assert len(runtime.system_prompt_text) > 0, 'system prompt is empty'

    # 3. Middleware and LLM
    assert runtime.fs_middleware is not None, 'fs_middleware not initialized'
    assert runtime.llm is not None, 'LLM not initialized'
    assert runtime.tool_emulator is not None, 'tool_emulator not initialized'
    assert runtime.retry_middleware is not None, 'retry_middleware not initialized'

    # 4. Pool, checkpointer, store
    assert runtime.pool is not None, 'pool not initialized'
    assert runtime.checkpointer is not None, 'checkpointer not initialized'
    assert runtime.store is not None, 'store not initialized'

    # 5. Agent
    assert runtime.agent is not None, 'agent not created'


def test_store_cli_inspect_session():
    """Verify store_cli inspect and inspect_session commands work.

    Requires: Postgres with seeded data.
    This test may fail if Postgres is unreachable or store is empty.
    """
    from dev_tools.store_cli import inspect_store, inspect_session_store

    # inspect should run without error (may print 'No items found')
    inspect_store('filesystem', show_size=False, show_dates=False, preview_chars=0)

    # inspect_session should run without error
    inspect_session_store('737280d4-4a28-4427-b851-de581174c3eb')
