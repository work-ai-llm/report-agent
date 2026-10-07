# PR: A2A Client - one client, full lifecycle

Status: **DRAFT — A2AClient unified, mock verification done (9/9), push isolated to `pr-push-receiver`.**

> This branch collects the A2A **client** code from stashes into one place, minus test-driving logic.
> Push receiver was split to `pr-push-receiver` (`client/push_receiver.py` + `docs/push_receiver.md` + `tests/client_push.py` reference, cherry-picked `b658669`/`66a5ab5` onto `master`). Unified `A2AClient` (`client/a2a_client.py`) is done.
> This doc records intent, what's gathered, remaining work, and dependencies.

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
- `tests/client_push.py` — `PushNotificationA2AClient` — **deleted** here, moved to `pr-push-receiver` (`b658669`/`66a5ab5` cherry-picked from `work-ai-llm/master`); kept there as wiring reference

From stash@{0} (proper single-client attempt) — lifecycle core, now unified:
- `client/a2a_client.py` — `A2AClient` (renamed from `A2ATaskService`, `tests/a2a_task_service.py` shim deleted in `db1d9d9`)
  - helpers `_create_send_request` (uuid `context_id`), `_extract_chunk_info` (handles `task` nested `id` FIX + `history` + `status_update`/`artifact_update`), `_process_stream`, `_is_streaming`
  - factory `A2AClient.create(server_url, streaming, httpx_client, polling)` mirrors `tests/acceptance/3_a2a_testing.py:254-260` (`A2ACardResolver` + `create_client` + `ClientConfig`)
  - public: `send_message` (3.1.1, first chunk only, respects `self._explicit_streaming` / `client._config.streaming && agent_card.capabilities.streaming`), `send_streaming_message` (3.1.2, full `_process_stream`), `subscribe_to_task` (3.1.6), `get_task` (3.1.3), `list_tasks` (3.1.4, `context_id/status/page_size/page_token/include_artifacts/history_length`), `cancel_task` (3.1.5), `poll_until_complete`
  - header trimmed to overview; streaming split rationale moved to `DEV_LOG.md:2026-09-07`

New on this branch (since `ddd5bdd` gather, push isolated to `pr-push-receiver` `e69502a`/`29ffe1d`):
- `client/a2a_client.py` — `A2AClient` (see above) + `client/__init__.py` marker
- `tests/mock/a2a_mocks.py` + `tests/mock/3_test_a2a_client_mock.py` (9 tests, ASGI `httpx.ASGITransport`, `FakeLangChainAgent`/`FakeStreamingAgent`, `global_execution_lock` fixture via `tests/mock/conftest.py`)
- `tests/mock/1_a2a_server_concurrent_task_connection.py` refactored to use `a2a_mocks.py`

Deleted / moved:
- `tests/a2a_task_service.py` (shim → `client/a2a_client.py`)
- `tests/client_streaming.py`, `tests/client_utils.py` (`07292c9`/`db1d9d9`)
- `client/push_receiver.py`, `docs/push_receiver.md`, `tests/mock/2_test_push_reciver_server.py`, `tests/client_push.py` → `pr-push-receiver` (cherry-pick `b658669`/`66a5ab5` onto `work-ai-llm/master`)

**Test-driving code explicitly EXCLUDED** (per gather decision): the `run_test_*`
functions, the mode-switch test runner, and any `3_a2a_testing.py` adaptations from the
stashes. The client is gathered alone; the tests belong to PR #9 (acceptance).

## What work is left

### Done (updated)
- [x] **Unify lifecycle core** — `A2ATaskService` -> `A2AClient` (`client/a2a_client.py`), `create` factory, `_is_streaming` flag (`_explicit_streaming` vs inferred `client._config.streaming && agent_card.capabilities.streaming` `agent/a2a_connector.py:357`), `_process_stream`/`_extract_chunk_info` preserved.
- [x] **send_message vs send_streaming_message split** — `send_message` 3.1.1 now true single `Task` via dedicated `streaming=False` client + `return_immediately=True` (not truncated stream `default_request_handler.py:449`), `send_streaming_message` 3.1.2 streamed (`DEV_LOG.md:2026-09-07`).
- [x] **list_tasks** — 3.1.4 wrapper `ListTasksRequest`/`ListTasksResponse`.
- [x] **Packaging** — moved from `tests/` to `client/`; `tests/a2a_task_service.py` removed.
- [x] **Reconcile PushNotificationConfig field variance** — via `TaskPushNotificationConfig` + `parse_push_payload:103` `_coalesce`, `a2a-sdk==1.1.0` pinned (now in `pr-push-receiver`).
- [x] **Verify subscribe/get/cancel signatures** — `SubscribeToTaskRequest(id)`/`GetTaskRequest(id)`/`CancelTaskRequest(id)` against `a2a/client/base_client.py`.
- [x] **Isolation** — rebase `--onto c649413 66a5ab5` dropped `b658669`/`66a5ab5`; push now only in `pr-push-receiver`.


### Still to do
- [x] **Mock-test A2AClient against server logic (without LLM API).** Ideally `FakeLangChainAgent` (as in `tests/mock/1_a2a_server_concurrent_task_connection.py`) + `InMemoryTaskStore` + `DefaultRequestHandler` but driven via `A2AClient` (not just `handler.on_message_send_stream`). Confirm `send_message` (1 chunk) / `send_streaming_message` (full stream) / `subscribe_to_task` / `get_task` / `cancel_task` / `list_tasks` / `poll_until_complete` work over the real lock/queue, and `cancel` releases lock. See Investigation.
- [ ] **Remaining spec** — 3.1.11 `getExtendedAgentCard` (`client.get_extended_agent_card`), 3.4 multi-turn (`context_id`/`task_id` continuation, `input_required` — deferred per branch decision), and `restart/resubscribe` final-state fetch.
- [ ] **Real run** — live `agent/a2a_connector.py:369` server + `Mistral` (or mock) to confirm lifecycle + push + restart, replacing `MockA2AServer`.


## Dependencies

- **No dependency on PR-A (server)** — server stays lock-based on master; client mocks reuse `agent/a2a_connector.py` handler directly.
- **Provides the client that PR #9 (acceptance) depends on.** PR #9's `run_test_5` calls client methods (`send`/`get`/`cancel`/`subscribe`/`poll`/`list`) that this PR supplies. So this PR should land **before** PR #9 is finished.
- **No dependency on PR-X (Phoenix)** — independent.

> NOTE: dependency list / architecture are provisional and should be revised as the client gets implemented and I understand the SDK surface better.

## How to use this branch

- **Unified client:** `from client.a2a_client import A2AClient` — factory `await A2AClient.create("http://...:9999", streaming=True)` then `send_message` (single Task) / `send_streaming_message` / `subscribe_to_task` / `get_task` / `list_tasks` / `cancel_task` / `poll_until_complete`; verified `pytest tests/mock/3_test_a2a_client_mock.py` `9 passed` + `1_a2a_server_concurrent` `2 passed` over ASGI `httpx.ASGITransport` (`FakeLangChainAgent`, no DB/LLM).
- **Push:** see `pr-push-receiver` `docs/push_receiver.md:49` + `docs/PR-push-receiver.md` (isolated, `pytest 2_test... 8 passed`).
