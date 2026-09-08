# PR: A2A Client - one client, full lifecycle

Status: **DRAFT — A2AClient unified + push receiver done, mock verification of client still pending.**

> This branch collects all the A2A **client** code currently spread across git stashes
> into one place, minus the test-driving logic. Push receiver `client/push_receiver.py` is
> now implemented and fixation-ready (see `docs/push_receiver.md`); unified `A2AClient` (`client/a2a_client.py`)
> is implemented and committed (renamed from `A2ATaskService`), but not yet mock/live verified.
> This doc records intent, what's gathered, target architecture,
> remaining work, and dependencies.

---

## What this PR represents

A **single A2A client** able to do the full task lifecycle against the report agent:
**send / subscribe / get / cancel / push**, plus **restart/resubscribe** to fetch the
final state — without knowing server internals.

This resolves the evolution "2 clients needed -> 1 client, with modes", which is why the
earlier attempts split the client (stash@{2}) and wrote a service-shaped lifecycle class
(stash@{0}/@{1}). Both ideas are merged into `A2AClient` here.

## End goal (done-when)

- One `A2AClient`-style client that, over protobuf:
  - `send_message` (3.1.1) -> `send_streaming_message` (3.1.2) -> `SubscribeToTaskRequest` -> `GetTaskRequest` -> `CancelTaskRequest` -> `ListTasksRequest`
  - extraction of `text`, `task_id`, `context_id`, `chunk_type` correct for every
    `Task | Status | Artifact` (see `_extract_chunk_info`).
  - can **push** (via `PushNotificationConfig` webhook) as well as **stream**.
  - can **restart** to fetch final state after a drop/zombie.
- This is the client that the acceptance test (PR #9, `pr-a2a-acceptance`) drives:
  `run_test_5` needs task_ids, then cancel / poll_until_complete / subscribe.

## Current state on this branch

From stash@{2} (2-client attempt) — kept as reference, now cleaned:
- `tests/client_utils.py` — `BaseA2AClient` + `extract_text_heuristically` — **deleted** in `07292c9` (subset of `_extract_chunk_info`; `BaseA2AClient` superseded by `A2AClient.create`)
- `tests/client_streaming.py` — `StreamingA2AClient` (streaming mode) — **deleted** in `db1d9d9` (redundant with `A2AClient.create(streaming=True)` + `send_streaming_message`)
- `tests/client_push.py` — `PushNotificationA2AClient` (push webhook attempt) — **kept** as reference for wiring `A2AClient` + `PushNotificationReceiver` (see Remaining/References)

From stash@{0} (proper single-client attempt) — lifecycle core, now unified:
- `client/a2a_client.py` — `A2AClient` (renamed from `A2ATaskService`, `tests/a2a_task_service.py` shim deleted in `db1d9d9`)
  - helpers `_create_send_request` (uuid `context_id`), `_extract_chunk_info` (handles `task` nested `id` FIX + `history` + `status_update`/`artifact_update`), `_process_stream`, `_is_streaming`
  - factory `A2AClient.create(server_url, streaming, httpx_client, polling)` mirrors `tests/acceptance/3_a2a_testing.py:254-260` (`A2ACardResolver` + `create_client` + `ClientConfig`)
  - public: `send_message` (3.1.1, first chunk only, respects `self._explicit_streaming` / `client._config.streaming && agent_card.capabilities.streaming`), `send_streaming_message` (3.1.2, full `_process_stream`), `subscribe_to_task` (3.1.6), `get_task` (3.1.3), `list_tasks` (3.1.4, `context_id/status/page_size/page_token/include_artifacts/history_length`), `cancel_task` (3.1.5), `poll_until_complete`
  - header trimmed to overview; streaming split rationale moved to `DEV_LOG.md:2026-09-07`

New on this branch (since `ddd5bdd` gather):
- `client/push_receiver.py` — `PushNotificationReceiver` webhook (asyncio FastAPI+uvicorn, `port=0`, `ParsedPushEvent`, `wait_for_completion`, terminal-only `on_event`/`completed_*`) — see `docs/push_receiver.md`
- `client/__init__.py` — package marker
- `tests/mock/2_test_push_reciver_server.py` — 8 mock tests using real `BasePushNotificationSender`+`InMemoryPushNotificationConfigStore` (`MockA2AServer`), fixation proof for receiver — `pytest 8/8`

Deleted on this branch (cleanup `bc35eaa`/`db1d9d9`/`07292c9`):
- `tests/a2a_task_service.py` (shim, logic moved to `client/a2a_client.py`)
- `tests/client_streaming.py`, `tests/client_utils.py`

**Test-driving code explicitly EXCLUDED** (per gather decision): the `run_test_*`
functions, the mode-switch test runner, and any `3_a2a_testing.py` adaptations from the
stashes. The client is gathered alone; the tests belong to PR #9 (acceptance).

## What work is left

### Done (updated)
- [x] **Push receiver webhook** — `client/push_receiver.py` implemented, 8/8 mock tests pass.
- [x] **Unify lifecycle core** — `A2ATaskService` -> `A2AClient` (`client/a2a_client.py`), `create` factory, `_is_streaming` flag (`_explicit_streaming` vs inferred `client._config.streaming && agent_card.capabilities.streaming` `agent/a2a_connector.py:357`), `_process_stream`/`_extract_chunk_info` preserved.
- [x] **send_message vs send_streaming_message split** — `send_message` 3.1.1 now true send-and-forget (first `StreamResponse` only) vs `send_streaming_message` 3.1.2 streamed (`DEV_LOG.md:2026-09-07`). Previously alias, now routed via `_is_streaming`.
- [x] **list_tasks** — 3.1.4 wrapper `ListTasksRequest`/`ListTasksResponse`.
- [x] **Packaging** — moved from `tests/` to `client/`; `tests/a2a_task_service.py` removed.
- [x] **Reconcile PushNotificationConfig field variance** — via `TaskPushNotificationConfig` + `parse_push_payload:103` `_coalesce`, `a2a-sdk==1.1.0` pinned.
- [x] **Verify subscribe/get/cancel signatures** — `SubscribeToTaskRequest(id)`/`GetTaskRequest(id)`/`CancelTaskRequest(id)` against `a2a/client/base_client.py`.

### Newly discovered (recorded per request)
- [ ] **Push ↔ Client wiring reference retained but not yet implemented.** Actual `A2AClient` + `PushNotificationReceiver` composition deferred. References to copy when wiring: `tests/client_push.py` (client + webhook together, `threading.Thread` vs `push_receiver.py:226` asyncio `port=0`) and `docs/push_receiver.md:49` logic `PushNotificationReceiver.start() -> url -> SendMessageRequest(configuration.task_push_notification_config) / create_task_push_notification_config / ClientConfig(push_notification_config) -> wait_for_completion -> get_task`. Kept as reference, implementation choice stays open until wiring moment. This is intentionally not done before client mock verification.
- [ ] **Client mock verification gap before remaining integrations.** Before any `NotificationConfig` (3.1.7-10 `create/get/list/delete TaskPushNotificationConfig`) or push wiring and before `getExtendedAgentCard` (3.1.11), the current `A2AClient` needs mock/live proof against real server stack — specifically `agent/a2a_connector.py:369` (`DefaultRequestHandler`+`TaskStore`+`EventQueue`+`TaskUpdater`) with concurrency lock `global_execution_lock:68`, `TaskStore` persistence, cancellation via `ReportA2AExecutor.cancel:281`. No existing mock covers `A2AClient` end-to-end (see Investigation below). This is vague by nature and kept as documented next step; approach kept open (extend existing server mock vs new client mock).

### Still to do
- [ ] **Mock-test A2AClient against server logic (without LLM API).** Ideally `FakeLangChainAgent` (as in `tests/mock/1_a2a_server_concurrent_task_connection.py`) + `InMemoryTaskStore` + `DefaultRequestHandler` but driven via `A2AClient` (not just `handler.on_message_send_stream`). Confirm `send_message` (1 chunk) / `send_streaming_message` (full stream) / `subscribe_to_task` / `get_task` / `cancel_task` / `list_tasks` / `poll_until_complete` work over the real lock/queue, and `cancel` releases lock. See Investigation.
- [ ] **NotificationConfig methods** — 3.1.7 `create_task_push_notification_config`, 3.1.8 `get`, 3.1.9 `list`, 3.1.10 `delete` wrapping `client.create/get/list/delete_task_push_notification_config` (`a2a/client/client.py` / `a2a/client/base_client.py`) and wiring to `PushNotificationReceiver.url` (after client mock proof; implies `agent/a2a_connector.py:369` `push_config_store` integration).
- [ ] **Push lifecycle wiring** — composition `A2AClient` + `PushNotificationReceiver` (choose per-message `configuration`, post-send `create_...`, or `ClientConfig` per `docs/push_receiver.md:49`). Keep `client_push.py` as wiring sketch.
- [ ] **Remaining spec** — 3.1.11 `getExtendedAgentCard` (`client.get_extended_agent_card`), 3.4 multi-turn (`context_id`/`task_id` continuation, `input_required` — deferred per branch decision), and `restart/resubscribe` final-state fetch.
- [ ] **Real run** — live `agent/a2a_connector.py:369` server + `Mistral` (or mock) to confirm lifecycle + push + restart, replacing `MockA2AServer`.


## Dependencies

- **No dependency on PR-A (server)** — server stays lock-based on master; client mocks reuse `agent/a2a_connector.py` handler directly.
- **Provides the client that PR #9 (acceptance) depends on.** PR #9's `run_test_5` calls client methods (`send`/`get`/`cancel`/`subscribe`/`poll`/`list`) that this PR supplies. So this PR should land **before** PR #9 is finished.
- **No dependency on PR-X (Phoenix)** — independent.

> NOTE: dependency list / architecture are provisional and should be revised as the client gets implemented and I understand the SDK surface better.

## How to use this branch

- **Push receiver:** usable now. See `docs/push_receiver.md:49` correct pattern `PushNotificationReceiver` + `A2AClient` composition; runnable via `pytest tests/mock/2_test_push_reciver_server.py` (8 passed) or import `client.push_receiver`.
- **Unified client:** `from client.a2a_client import A2AClient` — factory `await A2AClient.create("http://...:9999", streaming=True)` then `send_message`/`send_streaming_message`/`list_tasks`/etc. Mock verification pending (see Investigation); live run via `tests/acceptance/3_a2a_testing.py` pattern once server is up.
