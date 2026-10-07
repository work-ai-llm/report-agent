"""Acceptance test 9: agent run syncing - a turn survives an abandoned consumer.

Fixes #20. Companion to ``6_streamlit.py`` (which covers the happy path: prompt,
stream, render, persist). This one covers the failure path.

## The bug

A Streamlit script run is linear. Any widget interaction queues a rerun, and the
queued rerun unwinds the current script mid-execution. The agent turn was never
affected - ``RealAgentRunnerAdapter`` schedules the LangGraph stream onto the
persistent ``EventLoopThread`` - but the *persistence* was done by the script
thread. So a killed script lost the turn from the UI permanently, while the agent
happily kept going and kept checkpointing. The browser and the checkpointer
diverged and never reconciled.

## How this test simulates the interruption

Honestly, and at the layer where it can be reproduced deterministically:
``AppTest.run()`` is synchronous and blocks until the script finishes, so a test
cannot click a widget from a second thread and kill a run in flight.

Instead this test does what the killed script effectively did - **abandon the
consumer mid-turn and never read the result**:

1. Start a turn through the real service and poll a couple of events.
2. Drop the consumer on the floor (no further polling, no ``run()`` generator).
   This is a strict superset of "the script died": the agent keeps streaming and
   persisting with nobody watching.
3. Assert the run reaches a terminal state on its own.
4. Assert the UI-namespace row is **complete** - user prompt included, final
   assistant reply present - because the runner persisted it from the event-loop
   thread rather than from the consumer.
5. Build a *fresh* service, as a rerun / page reload would, and assert it reads
   the same transcript back.

Step 5 is the reconciliation that the UI performs via the run registry's revision
counter: the new session starts from nothing and converges on the full turn.

## Running it

Needs a real LLM. Defaults to SQLite for speed; set ``POSTGRES_URI`` to run
against Postgres instead (the DB choice is not what is under test here - the
persistence *timing* is).

    python -m pytest tests/acceptance/9_streamlit_agent_syncing.py -s -v
"""

from __future__ import annotations

import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# The agent needs a few tool calls to have a multi-step turn worth interrupting.
PROMPT = (
    "List the files in /context/interpretations/, read the first one, and then "
    "tell me in one sentence what it contains."
)

# How long to wait for an unattended run to finish. Generous because it includes
# real LLM latency and the turn is deliberately never consumed.
RUN_TIMEOUT = 900


def _wait_until(predicate, timeout: float = RUN_TIMEOUT, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_turn_survives_an_abandoned_consumer(monkeypatch, tmp_path):
    """A turn nobody reads is still persisted in full, and reads back intact."""
    # Force full real mode + isolated SQLite, regardless of the host env.
    monkeypatch.delenv("STREAMLIT_USE_MOCKS", raising=False)
    monkeypatch.delenv("STREAMLIT_USE_REAL_BACKEND", raising=False)
    monkeypatch.setenv("STREAMLIT_USE_REAL_AGENT", "1")
    monkeypatch.setenv("STREAMLIT_AGENT_PRINT", "1")
    monkeypatch.setenv("REPORT_AGENT_DB_PATH", str(tmp_path / "acceptance9.db"))
    monkeypatch.setenv("REPORT_AGENT_FORCE_SQLITE", "1")
    monkeypatch.setenv("POSTGRES_URI", "")
    # NOTE: AGENT_MODEL / AGENT_EMULATOR_MODEL are deliberately NOT overridden.
    # The model id is deployment-specific (this repo's .env points at a local
    # OpenAI-compatible server; pinning a hosted id here makes the agent fail with
    # a 404 and the turn still "passes", because a failed run also persists an
    # assistant row - see the error guard below).

    # A failed agent run still persists a row, so persisting alone proves
    # nothing. These markers are what the repo's model layer puts in its failure
    # text; treat any of them as "the LLM never ran".
    _FAILURE_MARKERS = (
        "Model call failed",
        "OpenAIModelNotFoundError",
        "Error code: 404",
        "Agent run failed",
    )

    log_path = tmp_path / "acceptance9.log"
    monkeypatch.setenv("STREAMLIT_AGENT_LOG", str(log_path))

    def _log(msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        print(line, flush=True)
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _tail(n: int = 25) -> None:
        try:
            lines = log_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            lines = []
        print(f"\n----- last {min(n, len(lines))} trace lines -----\n" + "\n".join(lines[-n:]), flush=True)

    from streamlit_ui.services import real_backend as rb
    from streamlit_ui.services.agent_service import PenTestAgentService
    from streamlit_ui.services.run_registry import REGISTRY

    rb._reset_backend_cache()
    REGISTRY.clear()
    try:
        runner, runtime = rb.get_real_backend()
        _log(
            f"runtime wired: llm={type(runtime.llm).__name__} "
            f"agent={type(runtime.agent).__name__} "
            f"store={'postgres' if runtime.pool is not None else 'sqlite'}"
        )
        if runtime.agent is None:
            _tail()
            raise AssertionError("real agent unavailable - check .env / STREAMLIT_USE_REAL_AGENT")

        thread_id = "acc9-abandoned-turn"
        ui_cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer, checkpoint_ns=rb._UI_CHECKPOINT_NS)
        store = rb.RealFileStoreAdapter(runner, runtime.store)
        adapter = rb.RealAgentRunnerAdapter(runner, runtime.agent, checkpointer=ui_cp)
        service = PenTestAgentService(
            thread_id=thread_id,
            checkpointer=ui_cp,
            file_store=store,
            agent_runner=adapter,
        )

        # 1. The user prompt must be durable the moment it exists, before any
        #    event has been produced.
        state = adapter.start(PROMPT, thread_id)
        immediate = (ui_cp.get(thread_id) or {}).get("messages", [])
        _log(f"rows immediately after start: {[m.get('type') for m in immediate]}")
        if len(immediate) != 1 or immediate[0].get("type") != "user":
            _tail()
            raise AssertionError(
                f"user prompt was not persisted at submit time: {immediate}"
            )

        # 2. Read a couple of events, then abandon the consumer mid-turn - this is
        #    what a killed Streamlit script run leaves behind.
        index = 0
        for _ in range(2):
            events, index, _rev = adapter.poll(thread_id, index)
            if events:
                _log(f"consumed {len(events)} event(s): {[e['type'] for e in events]}")
            if not state.is_running:
                break
            time.sleep(0.2)
        _log(f"abandoning consumer; run still active = {state.is_running}")
        del events, index  # the script run is gone; nothing polls from here on

        # 3. The run must finish unattended.
        if not _wait_until(lambda: not REGISTRY.is_running(thread_id)):
            _tail()
            raise AssertionError(f"run never completed within {RUN_TIMEOUT}s")
        snap = REGISTRY.get(thread_id).snapshot()
        _log(f"run finished unattended: status={snap['status']} error={snap['error']}")
        if snap["status"] != "done":
            _tail()
            raise AssertionError(f"run ended in {snap['status']}: {snap['error']}")

        # 4. The UI-namespace row must be complete despite the abandoned consumer.
        rows = (ui_cp.get(thread_id) or {}).get("messages", [])
        types = [m.get("type") for m in rows]
        _log(f"persisted rows: {types}")
        if not rows:
            _tail()
            raise AssertionError("the unattended turn was not persisted at all - this is #20")
        if types[0] != "user" or rows[0].get("content") != PROMPT:
            _tail()
            raise AssertionError(f"user prompt missing/corrupt at the head of the row: {rows[:2]}")
        assistant = [m for m in rows if m.get("type") == "assistant"]
        if not assistant or not (assistant[-1].get("content") or "").strip():
            _tail()
            raise AssertionError(f"final assistant reply never reached the row: {types}")
        # Guard against the vacuous pass: a run whose model calls all failed still
        # records an assistant row (the failure text). That is a broken LLM
        # wiring, not a persistence success, and must fail loudly.
        reply = assistant[-1]["content"]
        hit = [m for m in _FAILURE_MARKERS if m in reply]
        if hit:
            _tail()
            raise AssertionError(
                "the agent never actually ran the model - the persisted 'reply' is a "
                f"failure message ({hit}). Check LLM_BACKEND / LLM_BASE_URL / the model "
                f"id in .env. Reply was: {reply[:400]!r}"
            )
        tool_rows = [m for m in rows if m.get("type") == "tool_call"]
        _log(f"real reply ({len(reply)} chars) with {len(tool_rows)} tool_call row(s)")

        # The agent kept its own history too - the divergence that made #20
        # confusing is gone: both namespaces now describe the same turn.
        agent_cp = rb.RealCheckpointerAdapter(runner, runtime.checkpointer)
        _log(f"agent namespace rows for thread: {len(agent_cp.get(thread_id) or {})}")

        # 5. A fresh service (what a rerun / page reload builds) must read the
        #    same transcript back. This is the reconciliation the UI performs via
        #    the registry's revision counter.
        fresh = PenTestAgentService(
            thread_id=thread_id,
            checkpointer=rb.RealCheckpointerAdapter(
                runner, runtime.checkpointer, checkpoint_ns=rb._UI_CHECKPOINT_NS
            ),
            file_store=store,
            agent_runner=adapter,
        )
        reread = fresh.get_chat_history()
        _log(f"fresh service re-read {len(reread)} rows: {[m.get('type') for m in reread]}")
        if [m.get("type") for m in reread] != types:
            _tail()
            raise AssertionError(
                f"a fresh session read back a different transcript:\n"
                f"  stored: {types}\n  reread: {[m.get('type') for m in reread]}"
            )

        # 6. And a following turn must append to that history, not replace it.
        second = "Now tell me in one sentence what that file was about."
        list(service.send_message(second))
        after = [m.get("content") for m in (ui_cp.get(thread_id) or {}).get("messages", [])
                 if m.get("type") == "user"]
        _log(f"user turns after the follow-up: {len(after)}")
        if len(after) != 2:
            _tail()
            raise AssertionError(f"follow-up turn did not append cleanly: {after}")

        print(f"\n[ACCEPTANCE 9] thread: {thread_id}")
        print(f"[ACCEPTANCE 9] unattended run persisted: {types}")
        print(f"[ACCEPTANCE 9] final reply ({len(assistant[-1]['content'])} chars):")
        print(assistant[-1]["content"][:2000])
    finally:
        REGISTRY.clear()
        rb._reset_backend_cache()
        _log(f"done; trace written to {log_path}")