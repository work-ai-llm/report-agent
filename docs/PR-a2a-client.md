# PR: A2A Client - one client, full lifecycle

Status: **DRAFT / parked** — client code gathered from stashes, NOT yet unified into a
single working client.

> This branch collects all the A2A **client** code currently spread across git stashes
> into one place, minus the test-driving logic. It is NOT ready to merge. This doc records
> intent, what's gathered, the target architecture, remaining work, and dependencies.

---

## What this PR represents

A **single A2A client** able to do the full task lifecycle against the report agent:
**send / subscribe / get / cancel / push**, plus **restart/resubscribe** to fetch the
final state — without knowing server internals.

This resolves the evolution "2 clients needed -> 1 client, with modes", which is why the
earlier attempts split the client (stash@{2}) and wrote a service-shaped lifecycle class
(stash@{0}/@{1}). Both ideas are meant to be merged into one client here.

## End goal (done-when)

- One `A2ATaskService`-style client that, over protobuf:
  - `send_message` -> `SubscribeToTaskRequest` -> `GetTaskRequest` -> `CancelTaskRequest`
  - extraction of `text`, `task_id`, `context_id`, `chunk_type` correct for every
    `Task | Status | Artifact` (see `_extract_chunk_info`).
  - can **push** (via `PushNotificationConfig` webhook) as well as **stream**.
  - can **restart** to fetch final state after a drop/zombie.
- This is the client that the acceptance test (PR #9, `pr-a2a-acceptance`) drives:
  `run_test_5` needs task_ids, then cancel / poll_until_complete / subscribe.

## Current state gathered on this branch

From stash@{2} (2-client attempt) — kept as reference:
- `tests/client_utils.py` — `BaseA2AClient` interface + `extract_text_heuristically`
- `tests/client_streaming.py` — `StreamingA2AClient` (transport mode: stream)
- `tests/client_push.py` — `PushNotificationA2AClient` (transport mode: push webhook)

From stash@{0} (proper single-client attempt) — added as the lifecycle core:
- `tests/a2a_task_service.py` — `A2ATaskService` (send / subscribe / get / cancel /
  poll / _extract_chunk_info / _process_stream)

**Test-driving code explicitly EXCLUDED** (per gather decision): the `run_test_*`
functions, the mode-switch test runner, and any `3_a2a_testing.py` adaptations from the
stashes. The client is gathered alone; the tests belong to PR #9 (acceptance).

## What work is left (not a working single client yet)

- [ ] **Unify the two shapes into one client.** Decide the target architecture:
      `BaseA2AClient` is transport/mode-oriented; `A2ATaskService` is lifecycle-oriented.
      Proposed: make `A2ATaskService` the core, and fold stream/push as delivery per
      method (e.g. a `mode` option on send), and unify `_extract_chunk_info` with
      `extract_text_heuristically` into one extractor.
- [ ] Resolve packaging/location: currently under `tests/` (as the stashes had them).
      A reusable client likely belongs in a `client/` package or `dev_tools/`, not
      `tests/`. Decide during implementation.
- [ ] Reconcile the `PushNotificationConfig` field-name variance (`push_notification_config`
      vs `pushNotificationConfig`) against the pinned a2a version.
- [ ] Verify `client.subscribe` / `get_task` / `cancel_task` signatures against the SDK
      (the stash@{0} class notes "BaseClient requires a Protobuf request object").
- [ ] Wire `A2ATaskService` to actually consume the stream/push client (currently it takes
      a `client` via `__init__`, but that injection is not yet unified with the modes).
- [ ] Real run against the server to confirm lifecycle + push + restart.

## Dependencies

- **No dependency on PR-A (server)** — server stays lock-based on master.
- **Provides the client that PR #9 (acceptance) depends on.** PR #9's `run_test_5`
  calls client methods (`start_stream`/`send`, `get`, `cancel`, `subscribe`, `poll`)
  that these gathered files supply. So this PR should land **before** PR #9 is finished.
- **No dependency on PR-X (Phoenix)** — independent.

> NOTE: dependency list / architecture are provisional and should be revised as the
> client gets implemented and I understand the SDK surface better.

## How to use this branch

Gather/reference only. Pick the client pieces when implementing the unified client, or
finish the unification directly on this branch when ready. Nothing here is expected to
run green yet.
