"""
Acceptance tests for the `report-generation` skill (Scratchpad Assembly).

Feature: the report agent exposes its workflow as a skill (progressive
disclosure). The skill lives at `agent/skills/report-generation/SKILL.md`,
discovered by `SkillsMiddleware` (backend `/skills/` route -> real disk), and
the LLM *loads* the skill at runtime by calling `read_file` on its SKILL.md.

These tests prove the feature end-to-end:

  1. (offline, no LLM)  The skill is defined on disk with a valid SKILL.md
     frontmatter and is discoverable through the exact backend wiring the
     runtime uses (`CompositeBackend` with a `/skills/` FilesystemBackend route).
  2. (online, needs LLM) Running the agent on a report-ish task actually causes
     it to use the skill: a `read_file` tool call is issued against the
     SKILL.md path and the final reply reflects the Scratchpad Assembly
     workflow described in the skill.

Run (requires the local LLM endpoint, see dev_tools/model_server_cli.py):
    python tests/acceptance/9_skill_scratchpad_acceptance.py

Or via pytest (offline tests only work without a server; the online test is
only collected by pytest when invoked with a running agent):
    pytest tests/acceptance/9_skill_scratchpad_acceptance.py -v
"""
import asyncio
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # Windows console defaults to cp1252 which crashes on emoji/unicode → force UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

root_dir = Path(__file__).parent.parent.parent
sys.path.insert(0, str(root_dir))

SESSION_SKILL = "skill-scratchpad-001"

SKILL_NAME = "report-generation"
SKILL_PATH = f"/skills/{SKILL_NAME}/SKILL.md"

# Words that prove the skill's workflow, not just generic filler, drove the reply
SKILL_WORKFLOW_MARKERS = ("scratchpad", "assembly", "executive summary")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _skill_read_occurrences(messages) -> list[tuple[str, str]]:
    """Return (tool_name, file_path) for every tool call that read a skill file."""
    hits: list[tuple[str, str]] = []
    for m in messages:
        tool_calls = getattr(m, "tool_calls", None) or []
        for tc in tool_calls:
            name = tc.get("name", "")
            args = tc.get("args", {}) or {}
            path = str(args.get("file_path", "") or args.get("path", ""))
            if "SKILL.md" in path and "/skills/" in path:
                hits.append((name, path))
    return hits


async def _dump_session(rt, thread_id: str, title: str):
    """Print the full recorded message chain for a session (debug visibility)."""
    state = await rt.agent.aget_state({"configurable": {"thread_id": thread_id}})
    messages = state.values.get("messages", [])
    print(f"\n--- {title}: '{thread_id}' recorded {len(messages)} message(s) ---")
    for m in messages:
        role = getattr(m, "type", type(m).__name__)
        content = getattr(m, "content", "")
        if getattr(m, "tool_calls", None):
            extra = [f"{tc.get('name')}({tc.get('args')})" for tc in m.tool_calls]
            print(f"  [{role}] tool_calls: {extra}")
        printable = str(content).replace("\n", " \\n ")
        snippet = printable if len(printable) <= 300 else printable[:300] + " ..."
        print(f"  [{role}]: {snippet}")
    print("-" * 60)


# ---------------------------------------------------------------------------
# offline tests (no LLM server required)
# ---------------------------------------------------------------------------

def test_skill_file_definition():
    """SKILL.md exists with valid Agent-Skills frontmatter and the workflow body."""
    from agent.report_agent import SKILLS_DIR

    skill_dir = SKILLS_DIR / SKILL_NAME
    assert skill_dir.is_dir(), f"skill directory missing: {skill_dir}"
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file(), f"SKILL.md missing: {skill_md}"

    text = skill_md.read_text(encoding="utf-8")
    assert text.startswith("---"), "SKILL.md must start with YAML frontmatter"
    assert f"name: {SKILL_NAME}" in text, "frontmatter must declare the skill name"
    assert "description:" in text, "frontmatter must declare a description"
    assert "Scratchpad Assembly" in text, "workflow body must define the Scratchpad Assembly"
    assert "/scratchpad/" in text, "workflow body must reference the scratchpad"
    assert "/context/reports/" in text, "workflow body must reference report export"


def test_skill_discovery_through_backend():
    """The exact backend wiring the runtime uses finds the skill metadata."""
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
    from deepagents.middleware import SkillsMiddleware
    from agent.report_agent import SKILLS_DIR

    backend = CompositeBackend(
        default=StateBackend(),
        routes={
            "/context/": StateBackend(),  # placeholder; not exercised in this unit
            "/skills/": FilesystemBackend(root_dir=SKILLS_DIR, virtual_mode=True),
        },
    )
    mw = SkillsMiddleware(backend=backend, sources=[("/skills/", "Report Agent")])
    update = mw.before_agent(state={}, runtime=None, config={})
    assert update is not None, "skill discovery returned no state update"

    skills = update["skills_metadata"]
    names = [s["name"] for s in skills]
    assert SKILL_NAME in names, f"skill '{SKILL_NAME}' not discovered; got {names}"

    (skill,) = [s for s in skills if s["name"] == SKILL_NAME]
    assert skill["path"] == SKILL_PATH, f"skill path mismatch: {skill['path']}"
    assert skill["allowed_tools"], "skill must declare allowed-tools"


def test_runtime_exposes_skills_middleware():
    """An initialized pool of middleware layers wires SkillsMiddleware before the FS."""
    from agent.report_agent import AgentRuntime

    rt = AgentRuntime()
    rt._init_llm()        # constructs model clients only (no network calls)
    rt._init_middleware()  # builds FS + skills middleware

    assert rt.skills_middleware is not None, "skills_middleware not created"
    assert "/skills/" in rt.skills_middleware.sources, "skills source must be /skills/"
    assert rt.skills_middleware.system_prompt_template is not None, "skills must inject a prompt fragment"


# ---------------------------------------------------------------------------
# online test (requires the local LLM endpoint)
# ---------------------------------------------------------------------------

async def run_skill_usage_check(rt) -> bool:
    """The agent must load the skill: read_file on SKILL.md, then answer from its workflow."""
    config = {"configurable": {"thread_id": SESSION_SKILL}}
    prompt = (
        f"You have a '{SKILL_NAME}' skill in your skills library. First use your "
        f"read_file tool to read its SKILL.md instructions ({SKILL_PATH}), then explain "
        f"the Scratchpad Assembly workflow it defines: name each phase in order and "
        f"what it produces."
    )
    print("=" * 60)
    print(f"[Test - User]: {prompt}")
    print("=" * 60)
    result = await rt.agent.ainvoke({"messages": [{"role": "user", "content": prompt}]},
                                    config=config)
    answer = str(result["messages"][-1].content)
    print(f"[Test - Agent]: {answer}")

    state = await rt.agent.aget_state(config)
    messages = state.values.get("messages", [])
    hits = _skill_read_occurrences(messages)

    low = answer.lower()
    marker_hits = [m for m in SKILL_WORKFLOW_MARKERS if m in low]

    ok = bool(hits) and len(marker_hits) >= 1
    print(f"[Test - Result]: {'PASS' if ok else 'FAIL'}")
    print(f"  read_file on a skill: {len(hits)} -> {hits}")
    print(f"  workflow markers in answer: {marker_hits} ({len(marker_hits)}/{len(SKILL_WORKFLOW_MARKERS)})")
    await _dump_session(rt, SESSION_SKILL, "Skill usage session")
    return ok


async def run_all() -> bool:
    from agent.report_agent import AgentRuntime

    rt = AgentRuntime()
    await rt.initialize(debug_mode=True, force_sqlite=True)

    results = {
        "skill usage (read_file on SKILL.md + workflow-aware reply)": await run_skill_usage_check(rt),
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
    print("=" * 60)
    print("STARTING SKILL SCRATCHPAD ACCEPTANCE TEST")
    print("=" * 60)
    try:
        ok = asyncio.run(run_all())
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
        sys.stdout.flush()
        import os
        os._exit(1)