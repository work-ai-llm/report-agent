# PR: Push Receiver — isolated webhook (draft)

Status: **DRAFT / isolated — cherry-picked from `pr-a2a-client` (`b658669` + `66a5ab5`), not yet wired to `A2AClient`.**

> This branch isolates the push-receiver piece that previously lived inside `pr-a2a-client`. It contains only `client/push_receiver.py`, `tests/client_push.py` (reference), `tests/mock/2_test_push_reciver_server.py` and `docs/push_receiver.md`. No `A2AClient` lifecycle changes. Draft so it can be reviewed/merged independently of `pr-a2a-client`.

---

## What this PR represents

A **client-side webhook** that catches `POST {url} json=MessageToDict(to_stream_response(event))` from SDK `BasePushNotificationSender` (`agent/a2a_connector.py:369` + `InMemoryPushNotificationConfigStore`). Receiver is separate entity by design — `url` must be reachable by server, client owns `PushNotificationReceiver` lifetime and hands `TaskPushNotificationConfig(url)` via `SendMessageRequest(configuration=...)` / `create_task_push_notification_config` / `ClientConfig(push_notification_config=...)` (`BaseClient._apply_client_config:103`).

References kept for wiring later (not implemented here):
- `tests/client_push.py` — earlier `PushNotificationA2AClient` sketch (client + webhook together, `threading.Thread:81`, `PushNotificationConfig` `104`) superseded by `client/push_receiver.py:226` asyncio `port=0`; kept as wiring sketch for `A2AClient` + `PushNotificationReceiver` composition.
- `docs/push_receiver.md:49` — “How unified `A2AClient` will use it — logic, not verified code” (guess, `url` → `wait_for_completion` → `get_task`).

## Files in this PR

- `client/__init__.py` + `client/push_receiver.py:PushNotificationReceiver` (FastAPI+uvicorn, `port=0`, `_find_free_port:72` TOCTOU note, `_coalesce:83` camel/snake, `ParsedPushEvent:91`, `parse_push_payload:103` `task`/`statusUpdate|status_update`/`artifactUpdate`, `_is_terminal_state:203` `TERMINAL_TASK_STATES`, `webhook POST:284` token `X-A2A-Notification-Token` optional, `await queue.put:335` outside `_lock:321`, `wait_for_completion:476`, `clear:539`)
- `tests/mock/2_test_push_reciver_server.py` (8 tests via `MockA2AServer` wrapping real `InMemoryPushNotificationConfigStore`+`BasePushNotificationSender`, not hand-rolled `httpx.post`)
- `tests/client_push.py` (reference, not used)

## Tests / doc

- `docs/push_receiver.md:27` 8/8 `pytest` DO/DO-NOT (url, terminal-only `on_event`, camel/snake, `wait_for_completion`, per-task isolate, token optional, full sequence, 3 config flows)
- Run: `pytest tests/mock/2_test_push_reciver_server.py -v` (8 passed)

## Relationship to `pr-a2a-client`

Previously `docs/PR-a2a-client.md:8,59,48-51` documented push receiver inside the client PR. That section is **moved here**; `pr-a2a-client` will keep only a pointer (`see PR-push-receiver`). This cherry-pick (`b658669` + `66a5ab5`) was taken from `pr-a2a-client` onto `work-ai-llm/master` `e7318f0`, so diff vs `master` shows only push files (no `A2AClient` `client/a2a_client.py` / `a2a_mocks.py`). Merge order independent — if `pr-a2a-client` merges first, this PR’s diff auto-shrinks (same commits).

## Remaining (outside this PR)

- `A2AClient` lifecycle (`client/a2a_client.py`) stays in `pr-a2a-client`
- Wiring `A2AClient` + `PushNotificationReceiver` + `NotificationConfig` 3.1.7-10 (`create/get/list/delete`) deferred until `A2AClient` mock-proof (`pr-a2a-client` `3_test_a2a_client_mock.py`)

## How to use

```python
from client.push_receiver import PushNotificationReceiver
async with PushNotificationReceiver(port=0) as rec:
    url = rec.url  # give to server via TaskPushNotificationConfig(url=url)
    # ... send via A2AClient, then await rec.wait_for_completion(task_id)
```
See `docs/push_receiver.md:49` for unverified composition sketch.
