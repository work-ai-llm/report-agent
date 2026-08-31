# PR: Arize Phoenix local tracing fallback (experimental)

Status: **DRAFT / parked** — no implementation yet. Scaffold only.

> This file documents the intended PR so the work can "hang" in the project as a
> clean feature branch and be picked up later, without blocking or mixing with the
> A2A server / client / acceptance work.

## Goal (done-when)

Drop-in, dependency-light alternative to Langfuse for local tracing. No Docker, no
remote server. Uses `arize-phoenix` + OTEL writing to **local sqlite**.

- `agent/tracing.py` exists with:
  - `from phoenix.otel import register; register()`
  - `LangChainInstrumentor().instrument()`
  - import-safe, **no side effects at import time** (same rule enforced for `AgentRuntime`)
- Wired into `AgentRuntime` (`report_agent.py`) with a **graceful fallback** so a
  missing/broken phoenix does not break the agent (mirror of the langfuse fallback).
- Mock tests pass (no real agent / LLM / phoenix server).
- 1 real cheap LLM call produces a trace visible in the Phoenix UI.

## Motivation / why now

- ROADMAP.md "arize phoenix tracing (SQLite, no docker)" section, unchecked.
- Concrete trio of currently-mine stashes resolves to: server = done (do not touch),
  client (PR-B) and acceptance (PR-C) parked. Phoenix is independent, so it's the
  easiest first PR.

## Open questions to settle during implementation

1. Wire into `_init_langfuse()` as a *join* (run alongside) vs a 1:1 *swap*.
2. Keep or remove the manual langfuse span code in `a2a_connector.py:179`
   (`runtime.langfuse` start_as_current_observation). Does auto-instrumentation make it obsolete?
3. Whether `.runtime` langfuse-mock-based tests (used by the A2A concurrency PR) must
   be updated in tandem if the tracing object injected into `runtime` changes.

## Dependencies / ordering

- **Independent** of PR-A (server), PR-B (client), PR-C (acceptance).
- Per ROADMAP.md:119, **merge only after the a2a mock concurrency work is in master.**
- Watch for SQLite file coupling: Phoenix writes its own local sqlite; the agent uses a
  shared `agent_session.db` (WAL + busy_timeout). Verify no "database is locked"
  interplay before the acceptance test.

## Reference (ROADMAP.md:95-122)

- deps: `arize-phoenix` + `openinference-instrumentation-langchain` in pyproject.toml
- `report_agent.py:153-171` `_init_langfuse()` / `report_agent.py:351-356` callbacks
- mock span test with MagicMock LLM + mocked OTEL provider; import side-effect test
  mirroring `tests/acceptance/5_report_agent_structure.py:22` (`runtime.langfuse is None`);
  graceful-fallback test mirroring `report_agent.py:156-171`
- acceptance: real phoenix (sqlite) + 1 real cheap LLM call -> trace in UI
