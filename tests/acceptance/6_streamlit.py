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
``[AGENT STREAM]`` / ``[TOOL CALL]`` / ``[TOOL RESULT]`` / ``[CNK]`` /
``[AGENT RESPONSE]`` lines, exactly like the A2A acceptance loop prints chunks.
All milestones AND the agent-stream lines are also written to
``acceptance6.log`` (``STREAMLIT_AGENT_LOG``) with flushed prints, so a killed
or frozen run still leaves a trace. Each AppTest script run is capped at 15
minutes (``RUN_TIMEOUT``); when it times out or fails, the last 25 trace lines
are dumped so you can see exactly where the run was stuck.

Run from the repo root with ``-s`` so the streamed output is visible (a working
``OPENROUTER`` key in ``.env``; models are pinned via ``AGENT_MODEL`` /
``AGENT_EMULATOR_MODEL`` to an OpenRouter free-tier instruct model):

    python -m pytest tests/acceptance/6_streamlit.py -s -v
"""

import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
APP_PATH = str(REPO_ROOT / "streamlit_ui" / "app.py")

RUN_TIMEOUT = 900  # seconds per AppTest script run (15 minutes)

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
    monkeypatch.setenv("POSTGRES_URI", "")
    # Pin an OpenRouter free-tier instruct model (openrouter/auto can route to a
    # reasoning model, which loops badly on an agentic scratchpad workflow).
    monkeypatch.setenv("AGENT_MODEL", "openrouter/free")
    monkeypatch.setenv("AGENT_EMULATOR_MODEL", "openrouter/free")

    log_path = tmp_path / "acceptance6.log"
    monkeypatch.setenv("STREAMLIT_AGENT_LOG", str(log_path))

    def _log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _dump_trace_tail(n: int = 25) -> None:
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        tail = "\n".join(lines[-n:])
        print(f"\n----- last {min(n, len(lines))} trace lines ({log_path}) -----\n{tail}\n", flush=True)

    def _run_app(at, step: str) -> None:
        _log(f"running script: {step} (cap {RUN_TIMEOUT}s) ...")
        try:
            at.run(timeout=RUN_TIMEOUT)
        except Exception as exc:  # includes AppTest's own script timeout
            _log(f"script failed/timed out during '{step}': {type(exc).__name__}: {exc}")
            _dump_trace_tail()
            no_run = "AppTest script did not finish" if "timeout" in str(exc).lower() else ""
            raise AssertionError(
                f"STUCK during '{step}' after {RUN_TIMEOUT}s ({no_run}). "
                f"See trace tail above and {log_path} for where it stopped."
            ) from exc
        _log(f"script finished: {step}")
        exc = at.exception
        # ElementList() is Streamlit AppTest's error-element container (st.error
        # calls etc.) — not a fatal script exception. Only fail on real types.
        if exc is not None and type(exc).__name__ not in ("ElementList",):
            _dump_trace_tail()
            raise AssertionError(f"app raised during '{step}': {exc}")

    from streamlit.testing.v1 import AppTest
    from streamlit_ui.services import real_backend as rb

    rb._reset_backend_cache()
    try:
        at = AppTest.from_file(APP_PATH)
        _run_app(at, "boot")
        _log(f"boot asserts... exception={at.exception!r}")
        assert at.session_state["current_thread_id"] is None
        _log("boot asserts OK: no exception, no active thread")

        # Prove the real LLM and agent are wired before any chat turn:
        runner, runtime = rb.get_real_backend()
        model = (
            getattr(runtime.llm, "model", None)
            or getattr(runtime.llm, "model_name", None)
            or getattr(runtime.llm, "name", "?")
        )
        _log(f"runtime wired: llm={type(runtime.llm).__name__} model={model} "
             f"agent={type(runtime.agent).__name__} store={'sqlite' if runtime.pool is None else 'postgres'}")

        _log(f"sending prompt: {PROMPT!r}")
        at.chat_input[0].set_value(PROMPT)
        _run_app(at, "chat turn (agent runs here)")
        _log(f"chat_messages count: {len(list(at.session_state['chat_messages']))}")

        messages = list(at.session_state["chat_messages"])
        _log("chat_messages: " + ", ".join(
            f"{m.get('type')}"
            + (f"({m['name']})" if m.get("type") == "tool_call" else "")
            + (f"[{len(m.get('content', ''))}ch]" if m.get("type") == "assistant" else "")
            for m in messages
        ))
        if not messages or messages[0]["type"] != "user":
            _dump_trace_tail()
            raise AssertionError(
                f"no user message in chat_messages after chat turn. Trace: {log_path}"
            )
        tool_calls = [m for m in messages if m.get("type") == "tool_call"]
        final = [m for m in messages if m.get("type") == "assistant"]
        if not tool_calls or not final:
            _dump_trace_tail()
            raise AssertionError(
                f"expected tool_call + assistant reply, got {len(tool_calls)} tool_calls "
                f"and {len(final)} assistant msgs. Trace: {log_path}"
            )

        # Full-mode proof: beyond the UI display history (ns "ui"), the REAL
        # agent persisted its own LangGraph state under namespace "".
        thread_id = at.session_state["current_thread_id"]
        agent_cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer)
        assert thread_id in agent_cp.list_threads()
        _log(f"agent's own checkpoint found for thread {thread_id}")

        print(f"\n[ACCEPTANCE 6] thread: {thread_id}")
        print(f"[ACCEPTANCE 6] tool calls: {[t['name'] for t in tool_calls]}")
        print(f"[ACCEPTANCE 6] assistant reply ({len(final[0]['content'])} chars):")
        print(final[0]["content"][:4000])
    finally:
        rb._reset_backend_cache()
        _log(f"done; trace written to {log_path}")