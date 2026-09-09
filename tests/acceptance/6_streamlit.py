"""Acceptance test 6: the REAL agent driven through the Streamlit UI.

Mirrors ``tests/acceptance/3_a2a_testing.py`` test 1 - "Initialize the report
generation for the recon phase. Follow the scratchpad assembly workflow." - but
replaces the remote A2A client with a real user-style interaction: the test
types the same prompt into the Streamlit chat box (via AppTest) and watches the
agent work.

What it verifies:
- the app boots in full real mode (``STREAMLIT_USE_REAL_AGENT=1``),
- a chat turn reaches the real LangGraph agent through the UI,
- the agent's tool calls stream into the UI as ``tool_call`` entries,
- the agent's final reply is rendered and persisted in ``chat_messages``,
- the agent's own checkpoints survive in the real checkpointer (namespace "").

Agent output is printed to stdout (``STREAMLIT_AGENT_PRINT=1``) as
``[TOOL CALL]`` / ``[TOOL RESULT]`` / ``[CNK]`` / ``[AGENT RESPONSE]`` lines,
exactly like the A2A acceptance loop prints chunks.

Run from the repo root (needs a working ``MISTRAL_API_KEY`` in ``.env``,
reachable by the model configured in ``agent/report_agent.py``):

    python -m pytest tests/acceptance/6_streamlit.py -s -v
"""

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_PATH = str(REPO_ROOT / "streamlit_ui" / "app.py")

PROMPT = (
    "Initialize the report generation for the recon phase. "
    "Follow the scratchpad assembly workflow."
)


def test_streamlit_full_agent_scratchpad_workflow(monkeypatch, tmp_path):
    """Boot the UI and run the recon scratchpad workflow as a user would."""
    # Force full real mode, isolated DB, and streamed agent output to stdout,
    # regardless of the host's env / pytest collection order.
    monkeypatch.delenv("STREAMLIT_USE_MOCKS", raising=False)
    monkeypatch.delenv("STREAMLIT_USE_REAL_BACKEND", raising=False)
    monkeypatch.setenv("STREAMLIT_USE_REAL_AGENT", "1")
    monkeypatch.setenv("STREAMLIT_AGENT_PRINT", "1")
    monkeypatch.setenv("REPORT_AGENT_DB_PATH", str(tmp_path / "acceptance6.db"))

    from streamlit.testing.v1 import AppTest
    from streamlit_ui.services import real_backend as rb

    rb._reset_backend_cache()
    try:
        at = AppTest.from_file(APP_PATH)
        at.run(timeout=900)
        assert not at.exception, f"App failed to boot: {at.exception}"
        assert at.session_state["current_thread_id"] is None

        at.chat_input[0].set_value(PROMPT).run(timeout=900)
        assert not at.exception, f"Chat run failed: {at.exception}"

        messages = list(at.session_state["chat_messages"])
        assert messages and messages[0]["type"] == "user", messages
        tool_calls = [m for m in messages if m.get("type") == "tool_call"]
        final = [m for m in messages if m.get("type") == "assistant"]
        assert tool_calls, f"expected tool_call events, got: {messages}"
        assert final, f"expected an assistant reply, got: {messages}"

        # Full-mode proof: beyond the UI display history (ns "ui"), the REAL
        # agent persisted its own LangGraph state under namespace "".
        thread_id = at.session_state["current_thread_id"]
        runner, runtime = rb.get_real_backend()
        agent_cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer)
        assert thread_id in agent_cp.list_threads()

        print(f"\n[ACCEPTANCE 6] thread: {thread_id}")
        print(f"[ACCEPTANCE 6] tool calls: {[t['name'] for t in tool_calls]}")
        print(f"[ACCEPTANCE 6] assistant reply ({len(final[0]['content'])} chars):")
        print(final[0]["content"][:4000])
    finally:
        rb._reset_backend_cache()