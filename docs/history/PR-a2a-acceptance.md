# PR: A2A Server - Acceptance Test (test 5, full-stack proof)

Status: **DRAFT / parked** — gathered, not yet a working/complete acceptance proof.

> This branch collects the A2A **acceptance test** logic currently sitting across git
> stashes so it lives in one place, as a recoverable feature branch. It is NOT ready to
> merge. This doc records intent, current state, remaining work, and dependencies so the
> work can hang here and be finished once the client (PR-B) exists.

---

## What this PR represents

The "grand" acceptance proof of the full A2A stack: **real client + real server + real
concurrency lock working end-to-end**, driven as one large lifecycle test (test 5).

Where this came from: `tests/acceptance/3_a2a_testing.py` currently stops at test 3. The
more complete test 4 / test 5 logic lived in stashes `@{0}` and `@{1}`. This branch
gathers that logic so it can be finished and validated later.

## What it is trying to do (end goal / done-when)

- `stream -> working -> completed` with **2+ concurrent clients serialized** via the
  server's global execution lock (`agent/a2a_connector.py:68`, keep-alive loop around it).
- **Protocol preserved**: `thread_id = task.context_id` on the server side
  (`report_agent.py`), i.e. one client session maps to one agent thread.
- Lifecycle coverage: send / subscribe / get / cancel / poll, plus client **drop** and
  **zombie** (server keeps running a task whose client disconnected), and resubscribe
  to fetch the final state — without knowing server internals.
- One real run that passes (no mocks) is the acceptance bar, per project convention
  (mocks = dev aid only, e.g. `FakeLangChainAgent`).

Contains from stashes:
- `run_test_4` — sequential-execution stress (N concurrent tasks queued one-by-one).
- `run_test_5` (x2 variants) — the concurrency/drop/lifecycle scenarios.

## Current state on this branch (honest)

- `tests/acceptance/3_a2a_testing.py` has `run_test_0..4` and **two `run_test_5`
  definitions** (later shadows earlier in Python) plus helpers `extract_chunk_info`,
  `send_and_get_response`, `stream_to_completion`.
- There is leftover dead/broken code: a dead `task_id`/`subscribe_to_task` branch inside
  `send_and_get_response` (`client.subscribe_to_task(...)` doesn't exist on the SDK), and
  duplicated mid-file `import`/`from a2a.types...` statements.
- `agent/a2a_connector.py` is **unchanged from master** (server reverted; the scheduler
  architecture from stash@{1} was deliberately abandoned — the lock is enough).
- NOT merged in: stash@{0}'s `A2ATaskService` client class and stash@{2}'s
  stream/push client files. Client code belongs to PR-B, not this acceptance branch.

## What work is left (not a final acceptance test yet)

The gathered test logic is **fairly complete in shape but not yet a true acceptance
test** for the full A2A idea. Remaining work when this PR is picked up again:

- [ ] Pick/merge the two `run_test_5` variants into one coherent lifecycle test, or
      rename them (currently one shadows the other).
- [ ] Remove dead code: the non-existent `client.subscribe_to_task` branch in
      `send_and_get_response`, and the duplicated mid-file imports.
- [ ] Rewrite helpers to call the **real A2A client** (PR-B: send / subscribe / get /
      cancel / poll / restart) instead of SDK internals masked as helpers.
- [ ] Strip/correct assumptions that no longer hold (e.g. "Check server logs for
      'Skipping task...'" text, old `streaming=False` comments).
- [ ] Reconcile with the server's actual behavior: with the **lock** model a dropped
      client cancels the task (vs the abandoned scheduler which survived drops). Decide
      which drop/zombie semantics the acceptance proof actually asserts.
- [ ] Wire the chosen `run_test_5` into the CLI dispatch (`--test 5`) predictably.
- [ ] One real, pass-on-first full run against a real server (no mocks).

## Dependencies (subject to revision as I understand the code better)

- **Only firm dependency for now: PR-B (the A2A client).** Test 5 calls client methods
  (`start_stream`/`send`, `get`, `cancel`, `subscribe`, `poll`) that don't exist on
  master — those live in the parked client PR. Without PR-B, test 5 cannot run.

> NOTE (PLACEHOLDER, may be revised during development): other edges may surface once
> the client and test are wired, e.g. the exact server drop/zombie contract, and whether
> any tracing (Phoenix/PR-X) feeds into observing the flow. Keep this list editable.

- **No** dependency on PR-A (server) — server logic stays as master (lock-based).
- **No** dependency on PR-X (Phoenix tracing) — independent.

## How to use this branch

Pure gather/recovery: nothing here is meant to run as a green test yet. Cherry-pick the
test logic you need, or use the branch as a reference while building PR-B, then finish
this acceptance PR on top.
