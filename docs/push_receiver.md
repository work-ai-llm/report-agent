# Push Receiver (client webhook) - `client/push_receiver.py`

Status: **implemented, mock-tested (8/8 `pytest`)** - not yet wired into unified `A2ATaskService`, fixation ready.

> This doc reflects what the receiver *is*, what tests prove, and what they explicitly do NOT claim.

## What it is

- **Receiver, not sender.** Sender is complete in SDK `a2a/server/tasks/base_push_notification_sender.py:BasePushNotificationSender` + `InMemoryPushNotificationConfigStore` wired in `agent/a2a_connector.py:369`. Receiver is the *webhook* the **user client** runs to catch `POST {url} json=MessageToDict(to_stream_response(event))` where `event` is `Task|TaskStatusUpdateEvent|TaskArtifactUpdateEvent` as `StreamResponse` (`a2a/utils/proto_utils.py:to_stream_response`).

- **Separate entity by design.** `POST` URL must be reachable by the A2A server. Client owns lifecycle: `receiver = PushNotificationReceiver(port=0); url = await receiver.start();` give `TaskPushNotificationConfig(url=url)` to server via `SendMessageRequest(configuration=...)` or `client.create_task_push_notification_config` or `ClientConfig(push_notification_config=...)` (`BaseClient._apply_client_config:103`). Receiver stays in `client/`; `A2ATaskService` (`tests/a2a_task_service.py`) will own the A2A `client` — interaction is composition, not inheritance.

- **Package:** `client/__init__.py` + `client/push_receiver.py:PushNotificationReceiver` (asyncio-native FastAPI+uvicorn, `port=0` ephemeral via `_find_free_port:72`, no `threading.Thread`).

## Files

- `client/push_receiver.py:72` `_find_free_port` - ephemeral port; TOCTOU race noted `push_receiver.py:75`, retry on `OSError` for prod.
- `client/push_receiver.py:83` `_coalesce` - handles `taskId/task_id` camel/snake.
- `client/push_receiver.py:91` `ParsedPushEvent` - normalized view.
- `client/push_receiver.py:103` `parse_push_payload` - handles `task`/`statusUpdate|status_update`/`artifactUpdate` both casings; `message` branch kept but noted dead for push path (`push_notification_sender.py:10` never includes `Message`, `push_receiver.py:184`).
- `client/push_receiver.py:203` `_is_terminal_state` - mirrors SDK `TERMINAL_TASK_STATES={COMPLETED,CANCELED,FAILED,REJECTED}` (`active_task.py:83`), accepts both `TASK_STATE_X` and short, includes `REJECTED` added in fix #2.
- `client/push_receiver.py:226` `PushNotificationReceiver` - `host,port,path,token,external_host,on_event` (`on_event` called only on terminal, `push_receiver.py:342`).
- `client/push_receiver.py:284` `webhook POST` - validates `X-A2A-Notification-Token` only if `token` set (open by default), parses via `parse_push_payload:304`, stores `events`+`raw_events` for debug, stores `completed_task_ids`/`completed_events:332` and signals `_completion_events:337` only if terminal, `await queue.put:335` captured outside `_lock:321` (note `push_receiver.py:318` avoids deadlock, fix #1).
- `client/push_receiver.py:476` `wait_for_completion` - `await _completion_events[tid]:497` returns terminal `ParsedPushEvent`, fast-path if already completed `push_receiver.py:486`.
- `client/push_receiver.py:539` `clear` - notes waiters will timeout, not cancelled `push_receiver.py:542` (fix #5).

## Tests: `tests/mock/2_test_push_reciver_server.py` (uses *actual* SDK sender via `MockA2AServer` wrapping `InMemoryPushNotificationConfigStore`+`BasePushNotificationSender`, not hand-rolled `httpx.post`)

**DO (prove receiver -> client delivery, 8 passed):**

1. `test_receiver_exposes_url_for_client` - receiver binds and `url` is `http://host:port/webhook`, `GET /health` reachable. Proves client can obtain URL to give to server.
2. `test_receiver_catches_push_and_notifies_client` - `on_event` fired **only** on terminal `COMPLETED`; `WORKING`/`artifact` not notified, `get_events` keeps raw, `completed_events` holds only DONE. Proves `DONE` is ONLY notification client gets.
3. `test_receiver_normalizes_camel_and_snake_for_client` - same `Task` as camelCase and snakeCase both parse to same `task_id`. Proves client sees uniform view across SDK versions.
4. `test_client_wait_for_completion_via_receiver` - `wait_for_completion` stays pending on `WORKING`, unblocks on `COMPLETED`, returns terminal event. Proves client can `await` without polling.
5. `test_receiver_isolates_concurrent_tasks_for_client` - interleaved pushes for A,B, only B completed; `get_completed_task_ids=={B}`, `wait_for_completion` per `task_id` isolates. Proves `run_test_5` 5-task demultiplex.
6. `test_receiver_token_is_optional_security` - open receiver accepts any push; locked receiver rejects missing/wrong token 401, accepts `secret123` via raw header **and** via real sender `MockA2AServer(token="secret")` header `base_push_notification_sender.py:86`. Proves optional auth path.
7. `test_mock_server_complete_sequence_delivers_to_client` - full `_process_task` sequence `SUBMITTED task -> WORKING -> write_file tool -> artifact -> COMPLETED` via real sender; only terminal `on_event` to client. Proves end-to-end with real dispatch order.
8. `test_push_via_send_message_configuration` - `SendMessageRequest(configuration=SendMessageConfiguration(task_push_notification_config=...))` `HasField` branch `a2a_connector.py:304`, `create_task_push_notification_config` direct, `ClientConfig` auto-copy `BaseClient:103`. Proves the 3 canonical client flows lead to receiver completion.

**DO NOT (not claimed, requires real server run):**

- Does not start a real `agent/a2a_connector.py` server or real `Mistral` agent; `MockA2AServer` simulates `agent/a2a_connector.py:369` sender via in-memory store, not via `DefaultRequestHandler.on_message_send`.
- Does not test streaming `subscribe`/`get_task`/`cancel`/`list_tasks` - those are `A2ATaskService` lifecycle, covered by `pr-a2a-acceptance`.
- Does not test push persistence across receiver restarts (in-memory `completed_task_ids` lost).
- Does not test `REJECTED` terminal via actual server rejection (only unit `_is_terminal_state` includes it).
- Does not test TLS, public internet reachability, or `DatabasePushNotificationConfigStore`.
- `clear` does not cancel pending `wait_for_completion` - waiters timeout as noted `push_receiver.py:542`.

## How unified `A2ATaskService` (this branch) will use it — logic, not verified code

> **Note:** This is currently just a guess — real implementation may differ in logic (method names, move to `client/a2a_client.py`, helper location), but overall picture should stay as documented here.

Logic (composition, receiver stays in `client/`):

1. Client owns both lifetimes — start `PushNotificationReceiver` first to get `url` (`push_receiver.py:366`), then create A2A `client` (`A2ACardResolver` + `create_client`) and wrap with `A2ATaskService` (`tests/a2a_task_service.py:26`, will move to `client/`).
2. Hand `url` to server via any one of the three canonical ways `a2a_connector.py:304`: per-message `SendMessageRequest.configuration.task_push_notification_config`, post-send `create_task_push_notification_config(task_id, url)`, or per-client `ClientConfig(push_notification_config=...)`.
3. Send — initial `Task` still returned via `send_message` stream to extract `task_id` (`_extract_chunk_info:48` handles nested `task.id`); remaining updates come via push, not stream.
4. Await `receiver.wait_for_completion(task_id)` (`push_receiver.py:476`) — unblocks only on terminal `COMPLETED/FAILED/CANCELED/REJECTED`, returns `ParsedPushEvent` for logging.
5. Fetch result via `A2ATaskService.get_task(task_id)` (`a2a_task_service.py:144` `GetTaskRequest`) / `inspect_store`.

Snippets (illustrative, not verified implementation):

```python
url = rec.url  # from PushNotificationReceiver
req.configuration.task_push_notification_config.CopyFrom(TaskPushNotificationConfig(url=url))
ev = await rec.wait_for_completion(task_id)  # terminal only
task = await svc.get_task(task_id)
```

Streaming mode stays `svc.send_message` / `svc.subscribe_to_task` / `svc.poll_until_complete:164`.
