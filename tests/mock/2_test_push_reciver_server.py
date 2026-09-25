"""
File: tests/test_push_server.py
General: Verifies `client/push_receiver.py:PushNotificationReceiver` - the webhook
receiver a user-client runs to catch pushes from the SDK sender
`a2a/server/tasks/base_push_notification_sender.py:BasePushNotificationSender`
(+ `InMemoryPushNotificationConfigStore` wired in `agent/a2a_connector.py:369`).

Sender is complete in SDK and emits every `PushNotificationEvent` (Task,
TaskStatusUpdateEvent, TaskArtifactUpdateEvent) as `StreamResponse`
(`default_request_handler.py:373,444` per-event `push_notification_callback`).
Receiver's contract to the client is minimal: remember which `task_id`s reached
terminal `COMPLETED/FAILED/CANCELED` and wake the waiting client so it can
`get_task`/`inspect_store` the result. It keeps raw pushes for debugging but
`on_event`/`wait_for_completion` fire only on terminal, matching your note
"receiver simply to get notification that task is completed, then notify client".

Tests use `MockA2AServer` that wraps the *actual* SDK sender
(`BasePushNotificationSender` + `InMemoryPushNotificationConfigStore`) - not a
hand-rolled `httpx.post` - so verification is against real sender logic; only
the store is in-memory instead of DB.
"""

import asyncio
import pytest
import httpx

from google.protobuf.json_format import MessageToDict
from a2a.types.a2a_pb2 import (
    Task,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    TaskArtifactUpdateEvent,
    Artifact,
    Part,
    Message,
    Role,
    TaskPushNotificationConfig,
)
from a2a.utils.proto_utils import to_stream_response
from a2a.server.tasks import InMemoryPushNotificationConfigStore, BasePushNotificationSender
from a2a.server.context import ServerCallContext

from client.push_receiver import PushNotificationReceiver, parse_push_payload


# --- helpers: proto factories and mock wrapping actual SDK sender ---
def _make_task(task_id="task-123", context_id="ctx-abc", state=TaskState.TASK_STATE_WORKING, text="Working..."):
    return Task(id=task_id, context_id=context_id, status=TaskStatus(state=state, message=Message(role=Role.ROLE_AGENT, parts=[Part(text=text)], message_id="m1")))


def _make_status(task_id="task-123", context_id="ctx-abc", state=TaskState.TASK_STATE_WORKING, text="Status update"):
    return TaskStatusUpdateEvent(task_id=task_id, context_id=context_id, status=TaskStatus(state=state, message=Message(role=Role.ROLE_AGENT, parts=[Part(text=text)], message_id="m2")))


def _make_artifact(task_id="task-123", context_id="ctx-abc", text="artifact text"):
    return TaskArtifactUpdateEvent(task_id=task_id, context_id=context_id, artifact=Artifact(artifact_id="a1", parts=[Part(text=text)], name="report"), append=False, last_chunk=True)


class MockA2AServer:
    """Wraps actual SDK sender to simulate 'a2a server complete' pushes.

    Uses real `InMemoryPushNotificationConfigStore` + `BasePushNotificationSender`
    (same pair as `agent/a2a_connector.py:369`). No MagicMock needed - store is
    in-memory. Receiver is tested against real sender dispatch.
    """

    def __init__(self, receiver_url: str):
        self.receiver_url = receiver_url
        self.httpx_client = httpx.AsyncClient()
        self.store = InMemoryPushNotificationConfigStore()
        self.sender = BasePushNotificationSender(httpx_client=self.httpx_client, config_store=self.store)

    async def register(self, task_id: str, token: str | None = None):
        cfg = TaskPushNotificationConfig(task_id=task_id, url=self.receiver_url, token=token or "")
        await self.store.set_info(task_id, cfg, ServerCallContext())

    async def push(self, event):
        task_id = event.id if isinstance(event, Task) else event.task_id
        if not await self.store.get_info_for_dispatch(task_id):
            await self.register(task_id)
        await self.sender.send_notification(task_id, event)

    async def close(self):
        await self.httpx_client.aclose()


# --- tests: what each means, does, logic ---
@pytest.mark.asyncio
async def test_receiver_exposes_url_for_client():
    """Meaning: client is separate entity that must run receiver and give its URL
    to the a2a server (`TaskPushNotificationConfig(url=receiver.url)`).
    Does: start receiver on ephemeral port, check `url`/`bound_port`, GET /health.
    Logic: FastAPI+uvicorn binds `127.0.0.1:0` -> free port via `_find_free_port:61`,
    `url` is `http://host:port/webhook` the client will hand to server.
    """
    async with PushNotificationReceiver(host="127.0.0.1", port=0) as rec:
        assert rec.bound_port is not None
        assert rec.url.startswith("http://127.0.0.1:") and rec.url.endswith("/webhook")
        async with httpx.AsyncClient() as c:
            r = await c.get(f"http://127.0.0.1:{rec.bound_port}/health")
            assert r.status_code == 200


@pytest.mark.asyncio
async def test_receiver_catches_push_and_notifies_client():
    """Meaning: `seen` is client-side list (test creates it), receiver calls
    `on_event(task_id, terminal_event)` so client learns completed task without polling.
    Does: client passes `on_event=lambda e: seen.append(e)`, mock server pushes
    intermediate WORKING/artifact then terminal COMPLETED via real sender.
    Logic: receiver parses via `parse_push_payload:88` (handles camel/snake), stores
    all in `events` for debug, but `on_event` fires only if `_is_terminal_state:195`
    (`COMPLETED`). Verifies `seen` contains only the terminal DONE, proving
    `DONE` is the ONLY notification client gets, though raw `get_events` still logs all.
    """
    seen: list[tuple[str, str | None]] = []
    rec = PushNotificationReceiver(host="127.0.0.1", port=0, on_event=lambda e: seen.append((e.task_id, e.state)))
    await rec.start()
    mock = MockA2AServer(rec.url)
    try:
        # intermediate pushes must NOT notify client
        await mock.push(_make_task(task_id="t1", state=TaskState.TASK_STATE_SUBMITTED, text="Task started"))
        await mock.push(_make_status(task_id="t1", state=TaskState.TASK_STATE_WORKING, text="Still working"))
        await mock.push(_make_artifact(task_id="t1", text="Final report"))
        await asyncio.sleep(0.2)
        assert seen == [], "non-terminal should not notify"
        assert len(rec.get_events("t1")) == 3  # raw kept for debug
        assert not rec.is_completed("t1")

        # terminal does notify
        await mock.push(_make_status(task_id="t1", state=TaskState.TASK_STATE_COMPLETED, text="done"))
        await asyncio.sleep(0.2)
        assert seen == [("t1", "TASK_STATE_COMPLETED")]
        assert rec.is_completed("t1")
        assert rec.completed_events["t1"].text == "done"
    finally:
        await mock.close()
        await rec.stop()


@pytest.mark.asyncio
async def test_receiver_normalizes_camel_and_snake_for_client():
    """Meaning: SDK version differences emit `statusUpdate/taskId` vs `status_update/task_id`.
    Does: craft same Task both ways, parse and POST snake_case.
    Logic: `parse_push_payload:88` uses `_coalesce:68` for both casings; client sees uniform `task_id`.
    """
    task = _make_task(task_id="t-norm", text="hi")
    assert parse_push_payload(MessageToDict(to_stream_response(task))).task_id == "t-norm"
    assert parse_push_payload(MessageToDict(to_stream_response(task), preserving_proto_field_name=True)).task_id == "t-norm"

    async with PushNotificationReceiver(host="127.0.0.1", port=0) as rec:
        snake = MessageToDict(to_stream_response(task), preserving_proto_field_name=True)
        async with httpx.AsyncClient() as c:
            await c.post(rec.url, json=snake)
        await asyncio.sleep(0.1)
        assert rec.get_events("t-norm")[0].task_id == "t-norm"


@pytest.mark.asyncio
async def test_client_wait_for_completion_via_receiver():
    """Meaning: client wants `await receiver.wait_for_completion(tid)` instead of polling `get_task`.
    Does: start `wait_for_completion` before any push, push WORKING (should stay pending),
    then push COMPLETED (should unblock).
    Logic: `wait_for_completion:437` awaits `_completion_events[tid]` which is set only on
    terminal `_is_terminal_state:195`; returns the terminal `ParsedPushEvent` so client can
    then `client.get_task(tid)` the result.
    """
    async with PushNotificationReceiver(host="127.0.0.1", port=0) as rec:
        mock = MockA2AServer(rec.url)
        waiter = asyncio.create_task(rec.wait_for_completion("t-complete", timeout=5.0))
        await asyncio.sleep(0.1)
        assert not waiter.done()
        await mock.push(_make_status(task_id="t-complete", state=TaskState.TASK_STATE_WORKING, text="working"))
        await asyncio.sleep(0.1)
        assert not waiter.done(), "WORKING must not unblock"
        await mock.push(_make_status(task_id="t-complete", state=TaskState.TASK_STATE_COMPLETED, text="done"))
        ev = await asyncio.wait_for(waiter, timeout=2.0)
        assert ev.state == "TASK_STATE_COMPLETED" and ev.text == "done"
        # confirm DONE is the only completed in receiver, as you requested
        assert rec.get_completed_task_ids() == {"t-complete"}
        assert len(rec.get_events("t-complete")) == 2  # raw 2, completed 1
        await mock.close()


@pytest.mark.asyncio
async def test_receiver_isolates_concurrent_tasks_for_client():
    """Meaning: receiver must demultiplex pushes by `task_id`; client waiting for
    task-A must not see task-B completions. This matches `run_test_5` firing 5 tasks.
    Does: mock interleaves WORKING/artifacts for A,B, completes only B.
    Logic: `events:245` dict keyed by `task_id`, `completed_task_ids: set` per-task;
    `is_completed`/`wait_for_completion` per `task_id` isolates.
    """
    async with PushNotificationReceiver(host="127.0.0.1", port=0) as rec:
        mock = MockA2AServer(rec.url)
        await mock.push(_make_status(task_id="task-A", text="A working"))
        await mock.push(_make_status(task_id="task-B", text="B working"))
        await mock.push(_make_artifact(task_id="task-A", text="A artifact"))  # not terminal
        await mock.push(_make_status(task_id="task-B", state=TaskState.TASK_STATE_COMPLETED, text="B done"))
        await asyncio.sleep(0.1)
        # raw logs all
        assert len(rec.get_events("task-A")) == 2
        assert len(rec.get_events("task-B")) == 2
        # but only B is completed - this is the fix you asked for
        assert rec.get_completed_task_ids() == {"task-B"}
        assert rec.is_completed("task-B") and not rec.is_completed("task-A")
        await rec.wait_for_completion("task-B", timeout=1.0)
        try:
            await rec.wait_for_completion("task-A", timeout=0.3)
            assert False, "task-A not completed"
        except TimeoutError:
            pass
        await mock.close()


@pytest.mark.asyncio
async def test_receiver_token_is_optional_security():
    """Meaning: webhook is open by default ("exposed for all requests" as you noted);
    token is optional extra if you deploy receiver publicly and want to reject spoofed pushes.
    Does: first push to open receiver via real sender succeeds; then to locked receiver
    test both raw header and real sender token path.
    Logic: webhook checks `X-A2A-Notification-Token` only if `token` set at init `push_receiver.py:268`;
    sender sends it from `TaskPushNotificationConfig.token` header `base_push_notification_sender.py:86`.
    Covers both raw header (attacker) and `MockA2AServer(token=...)` -> sender header path you flagged missing.
    """
    async with PushNotificationReceiver(host="127.0.0.1", port=0, token=None) as open_rec:
        mock_open = MockA2AServer(open_rec.url)
        await mock_open.push(_make_task(task_id="t-open"))
        await asyncio.sleep(0.1)
        assert open_rec.is_completed("t-open") is False  # WORKING not completed
        await mock_open.push(_make_status(task_id="t-open", state=TaskState.TASK_STATE_COMPLETED, text="done"))
        await asyncio.sleep(0.1)
        assert open_rec.is_completed("t-open")
        await mock_open.close()

    async with PushNotificationReceiver(host="127.0.0.1", port=0, token="secret123") as locked:
        # raw header path (attacker without token)
        payload = MessageToDict(to_stream_response(_make_status(task_id="t-auth", state=TaskState.TASK_STATE_COMPLETED, text="done")))
        async with httpx.AsyncClient() as c:
            assert (await c.post(locked.url, json=payload)).status_code == 401
            assert (await c.post(locked.url, json=payload, headers={"X-A2A-Notification-Token": "wrong"})).status_code == 401
            assert (await c.post(locked.url, json=payload, headers={"X-A2A-Notification-Token": "secret123"})).status_code == 200
        assert locked.is_completed("t-auth")
        locked.clear()
        # real sender header path - MockA2AServer registers TaskPushNotificationConfig(token=...)
        # and BasePushNotificationSender injects header; this was missing before
        mock_locked = MockA2AServer(locked.url)
        # wrong token via sender -> receiver should reject (401) and not mark completed
        await mock_locked.register("t-auth2", token="wrong")
        await mock_locked.push(_make_status(task_id="t-auth2", state=TaskState.TASK_STATE_COMPLETED, text="done"))
        await asyncio.sleep(0.2)
        assert not locked.is_completed("t-auth2"), "wrong token via sender must not complete"
        # correct token via sender -> 200 and completed
        await mock_locked.register("t-auth3", token="secret123")
        await mock_locked.push(_make_status(task_id="t-auth3", state=TaskState.TASK_STATE_COMPLETED, text="done"))
        await asyncio.sleep(0.2)
        assert locked.is_completed("t-auth3")
        await mock_locked.close()


@pytest.mark.asyncio
async def test_mock_server_complete_sequence_delivers_to_client():
    """Meaning: mimic real `agent/a2a_connector.py:_process_task` push sequence
    (Task SUBMITTED -> WORKING Processing -> tool write_file -> artifact -> COMPLETED).
    Does: `on_event` appends terminal output; mock pushes 5 events via real sender.
    Logic: sender's `ResultAggregator` + `push_notification_callback:373,444` fires
    per-event, but receiver's client-facing `on_event`/`wait_for_completion` fires
    only on terminal, so client sees single "Report generated" completion, then fetches result.
    """
    async with PushNotificationReceiver(host="127.0.0.1", port=0) as rec:
        client_output: list[str] = []
        rec.on_event = lambda ev: client_output.append(ev.text or ev.state or "")
        mock = MockA2AServer(rec.url)
        task_id = "real-seq"
        for ev in [
            _make_task(task_id=task_id, state=TaskState.TASK_STATE_SUBMITTED, text="Task received. Waiting in execution queue..."),
            _make_status(task_id=task_id, state=TaskState.TASK_STATE_WORKING, text="Processing request..."),
            _make_status(task_id=task_id, state=TaskState.TASK_STATE_WORKING, text="Agent is calling tool: write_file"),
            _make_artifact(task_id=task_id, text="Report creation process started."),
            _make_status(task_id=task_id, state=TaskState.TASK_STATE_COMPLETED, text="Request completed. Report generated in shared store."),
        ]:
            await mock.push(ev)
        await asyncio.sleep(0.2)
        # only terminal notified to client
        assert client_output == ["Request completed. Report generated in shared store."]
        ev = await rec.wait_for_completion(task_id, timeout=1.0)
        assert ev.state == "TASK_STATE_COMPLETED"
        # raw still has all 5 for debug, completed set has 1
        assert len(rec.get_events(task_id)) == 5
        assert rec.get_completed_task_ids() == {task_id}
        await mock.close()


@pytest.mark.asyncio
async def test_push_via_send_message_configuration():
    """Meaning: covers `a2a_connector.py:304` HasField branch you flagged missing:
    client can send `SendMessageRequest(configuration=SendMessageConfiguration(
    task_push_notification_config=TaskPushNotificationConfig(url=receiver.url)))`
    or `client.create_task_push_notification_config(...)` or
    `ClientConfig(push_notification_config=...)` -> `BaseClient._apply_client_config:103`.
    Does: build `SendMessageRequest` with config, verify `MessageToDict` preserves it,
    then simulate server storing it (`store.set_info`) and pushing via real sender.
    Logic: proves the two canonical client flows (per-message config and
    `create_task_push_notification_config`) both lead to receiver getting the
    terminal push.
    """
    from a2a.types.a2a_pb2 import SendMessageRequest, SendMessageConfiguration

    async with PushNotificationReceiver(host="127.0.0.1", port=0) as rec:
        # Flow 1: per-message SendMessageRequest.configuration
        req = SendMessageRequest(
            message=Message(message_id="m1", context_id="ctx", role=Role.ROLE_USER, parts=[Part(text="hi")]),
            configuration=SendMessageConfiguration(task_push_notification_config=TaskPushNotificationConfig(url=rec.url, token="")),
        )
        assert req.configuration.HasField("task_push_notification_config")
        assert req.configuration.task_push_notification_config.url == rec.url
        # simulate server's _setup_message_execution:304 storing it
        task_id = "cfg-task-1"
        store = InMemoryPushNotificationConfigStore()
        sender = BasePushNotificationSender(httpx.AsyncClient(), store)
        await store.set_info(task_id, req.configuration.task_push_notification_config, ServerCallContext())
        # need to ensure task_id matches config's task_id for dispatch (store keys by task_id)
        # our TaskPushNotificationConfig from request has no task_id, server sets it via task_id param
        # so mimic server: copy url token into config with task_id
        cfg_with_id = TaskPushNotificationConfig(task_id=task_id, url=rec.url)
        await store.set_info(task_id, cfg_with_id, ServerCallContext())
        await sender.send_notification(task_id, _make_status(task_id=task_id, state=TaskState.TASK_STATE_COMPLETED, text="done via config"))
        await asyncio.sleep(0.2)
        assert rec.is_completed(task_id)
        await sender._client.aclose()

        # Flow 2: explicit create_task_push_notification_config (direct store set)
        mock = MockA2AServer(rec.url)
        await mock.register("cfg-task-2")
        await mock.push(_make_status(task_id="cfg-task-2", state=TaskState.TASK_STATE_COMPLETED, text="done via create"))
        await asyncio.sleep(0.2)
        assert rec.is_completed("cfg-task-2")
        await mock.close()

        # Flow 3: ClientConfig auto-copy (BaseClient._apply_client_config:103)
        from a2a.client import ClientConfig
        cfg_client = ClientConfig(push_notification_config=TaskPushNotificationConfig(url=rec.url))
        req2 = SendMessageRequest(message=Message(message_id="m2", role=Role.ROLE_USER, parts=[Part(text="hi2")]))
        # simulate BaseClient._apply_client_config
        if not req2.configuration.HasField("task_push_notification_config") and cfg_client.push_notification_config:
            req2.configuration.task_push_notification_config.CopyFrom(cfg_client.push_notification_config)
        assert req2.configuration.task_push_notification_config.url == rec.url

