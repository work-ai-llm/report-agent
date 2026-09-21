"""
LLM API acceptance tests for the report agent (v2).

Runs against a local llama.cpp model server (see dev_tools/model_server_cli.py).
The backend is selected with ``--backend sqlite|postgres`` (default sqlite):

  python tests/acceptance/8_llm_api_acceptance.py --backend sqlite
  python tests/acceptance/8_llm_api_acceptance.py --backend postgres

Afterwards the sessions can be inspected without any LLM call - the same
thread ids work on both backends because each store is independent:

  python dev_tools/store_cli.py --sqlite list-sessions                      # sqlite run
  python dev_tools/store_cli.py --sqlite inspect-session --thread-id <id>   # sqlite run
  python dev_tools/store_cli.py list-sessions                               # postgres run
  python dev_tools/store_cli.py inspect-session --thread-id <id>            # postgres run

A postgres run that silently falls back to SQLite is reported and FAILS.

Tests:
  1. Smoke: a single "hi" turn.  Fastest possible "is the LLM endpoint even
     reachable / responding" check.
  2. Core sanity (agents calls, still cheap):
     a. System-prompt loading: ask the agent who/what it is.  A healthy reply
        reflects the pentest report-writer system prompt, NOT generic filler.
     b. Tool calling: ask it to inspect the virtual filesystem with `ls`.
        Verifies the tool layer + LLM tool-use actually work before any long
        workflow is attempted.
  3. NOTE: the full Scratchpad Assembly report lifecycle lives in the OLD
     tests/acceptance/1_test_agent.py (test_7_full_report_generation) and is
     *not* ported here.
     TODO(me): maybe add it here later as a long-running end-to-end workflow
     test ... or maybe not.  Only worth it once tests 1 + 2 stay green.
"""
import asyncio
import os
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # Windows console defaults to cp1252 which crashes on emoji/unicode → force UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

root_dir = Path(__file__).parent.parent.parent
sys.path.insert(0, str(root_dir))

# Thread/session names — usable later with store_cli:
#   python dev_tools/store_cli.py --sqlite inspect-session --thread-id <name>
SESSION_HI = "llm-api-hi-001"            # test 1: smoke
SESSION_SYSPROMPT = "llm-api-sysprompt-002"  # test 2a
SESSION_VFS = "llm-api-vfs-003"              # test 2b

# substrings that prove the system prompt is actually driving the reply
SYSPROMPT_MARKERS = ("findings", "template", "scratchpad", "report", "export", "pentest")


async def _dump_session(rt, thread_id, title: str):
    """Print the full recorded message chain for a session (debug visibility)."""
    state = await rt.agent.aget_state({"configurable": {"thread_id": thread_id}})
    messages = state.values.get("messages", [])
    print(f"\n--- {title}: '{thread_id}' recorded {len(messages)} message(s) ---")
    for m in messages:
        role = getattr(m, "type", type(m).__name__)
        content = getattr(m, "content", "")
        printable = str(content).replace("\n", " \\n ") if not isinstance(content, list) else str(content)
        snippet = printable if len(printable) <= 300 else printable[:300] + " ..."
        print(f"  [{role}]: {snippet}")
    print("-" * 60)


def _mention_count(text: str) -> int:
    low = text.lower()
    return sum(1 for m in SYSPROMPT_MARKERS if m in low)


async def run_smoke(rt) -> bool:
    """Test 1: trivial turn — endpoint up and generating?"""
    config = {"configurable": {"thread_id": SESSION_HI}}
    prompt = "hi"
    print("=" * 60)
    print(f"[Test 1 - User]: {prompt}")
    print("=" * 60)
    result = await rt.agent.ainvoke({"messages": [{"role": "user", "content": prompt}]},
                                    config=config)
    answer = result["messages"][-1].content
    print(f"[Test 1 - Agent]: {answer}")
    ok = bool(answer and len(str(answer)) > 0)
    print(f"[Test 1 - Result]: {'PASS' if ok else 'FAIL — empty reply'}")
    await _dump_session(rt, SESSION_HI, "Test 1 session")
    return ok


async def run_system_prompt_check(rt) -> bool:
    """Test 2a: does the reply reflect the pentest report-writer system prompt?"""
    config = {"configurable": {"thread_id": SESSION_SYSPROMPT}}
    prompt = ("Who are you and what is your primary purpose, based on your "
              "instructions? Answer in 2-3 sentences.")
    print("=" * 60)
    print(f"[Test 2a - User]: {prompt}")
    print("=" * 60)
    result = await rt.agent.ainvoke({"messages": [{"role": "user", "content": prompt}]},
                                    config=config)
    answer = str(result["messages"][-1].content)
    print(f"[Test 2a - Agent]: {answer}")
    hits = _mention_count(answer)
    ok = hits >= 2
    print(f"[Test 2a - Result]: {'PASS' if ok else 'FAIL'} — mentions "
          f"{hits}/len({len(SYSPROMPT_MARKERS)}) system-prompt markers: "
          f"{[m for m in SYSPROMPT_MARKERS if m in answer.lower()]}")
    await _dump_session(rt, SESSION_SYSPROMPT, "Test 2a session")
    return ok


async def run_vfs_tool_check(rt) -> bool:
    """Test 2b: can the agent actually call tools (ls on the virtual FS)?"""
    config = {"configurable": {"thread_id": SESSION_VFS}}
    prompt = ("Using your tools, list what is in /context, and name the tools "
              "you used. Keep the answer brief.")
    print("=" * 60)
    print(f"[Test 2b - User]: {prompt}")
    print("=" * 60)
    result = await rt.agent.ainvoke({"messages": [{"role": "user", "content": prompt}]},
                                    config=config)
    answer = str(result["messages"][-1].content)
    print(f"[Test 2b - Agent]: {answer}")

    state = await rt.agent.aget_state({"configurable": {"thread_id": SESSION_VFS}})
    messages = state.values.get("messages", [])
    tool_calls = sum(1 for m in messages if getattr(m, "type", "") == "tool")
    tool_used = any(getattr(m, "type", "") in ("tool", "ToolMessage") for m in messages)
    ok = tool_used and len(answer) > 0
    print(f"[Test 2b - Result]: {'PASS' if ok else 'FAIL'} — tool call messages "
          f"in session: {tool_calls}")
    await _dump_session(rt, SESSION_VFS, "Test 2b session")
    return ok


async def run_all(backend: str = "sqlite") -> bool:
    from agent.report_agent import AgentRuntime

    rt = AgentRuntime()
    await rt.initialize(debug_mode=True, force_sqlite=(backend == "sqlite"))

    # -- Backend verification ------------------------------------------------
    # report_agent._init_pool() silently FALLS BACK to SQLite when Postgres is
    # unreachable. A requested "postgres" run that landed on SQLite is NOT a
    # passing test, so detect it here and fail explicitly.
    selected = type(rt.checkpointer).__name__
    if backend == "postgres":
        backend_ok = rt.pool is not None
        print(f"[backend] requested=postgres selected={selected} "
              f"({'PASS' if backend_ok else 'FAIL - Postgres unreachable, runtime fell back to SQLite!'})")
    else:
        backend_ok = rt.pool is None
        print(f"[backend] requested=sqlite   selected={selected} "
              f"({'PASS' if backend_ok else 'FAIL - Postgres unexpectedly used'})")

    results = {
        "0. backend": backend_ok,
        "1. smoke (hi)": await run_smoke(rt),
        "2a. system prompt loaded": await run_system_prompt_check(rt),
        "2b. vfs tool calling": await run_vfs_tool_check(rt),
    }

    # Close async sqlite connections so store_cli can open the same file cleanly
    try:
        if rt.checkpointer is not None and hasattr(rt.checkpointer, "conn"):
            await rt.checkpointer.conn.close()
    except Exception:
        pass
    try:
        if rt.store is not None and hasattr(rt.store, "conn"):
            await rt.store.conn.close()
    except Exception:
        pass

    print("\n" + "=" * 60)
    print("SUMMARY")
    for name, passed in results.items():
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
    print("=" * 60)
    return all(results.values())


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(
        description="LLM API acceptance test for the report agent (v2) - drives "
                    "the real AgentRuntime against a local llama.cpp model server."
    )
    parser.add_argument(
        "--backend", choices=["sqlite", "postgres"], default="sqlite",
        help="Checkpointer backend to run against. sqlite = local "
             "agent/agent_session.db (override with REPORT_AGENT_DB_PATH); "
             "postgres = .env POSTGRES_URI. The run FAILS (exit 1) if postgres "
             "was requested but the runtime silently fell back to SQLite.",
    )
    args = parser.parse_args()

    print("=" * 60)
    print("STARTING LLM API ACCEPTANCE TEST (v2)")
    print(f"backend: {args.backend}")
    print("=" * 60)
    try:
        ok = asyncio.run(run_all(args.backend))
        print(f"ALL TESTS {'PASSED' if ok else 'FAILED'}")
        sys.exit(0 if ok else 1)
    except KeyboardInterrupt:
        print("\nINTERRUPTED")
        sys.exit(130)
    except Exception:
        # prevent lingering httpx/openai threads from hanging the process
        import traceback
        traceback.print_exc()
        print("ALL TESTS FAILED (exception)")
        os._exit(1)