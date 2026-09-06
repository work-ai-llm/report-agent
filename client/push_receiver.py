"""
Push Notification *Receiver* (webhook) for A2A.

**THIS IS NOT THE SENDER.** Sender is ``a2a.server.tasks.BasePushNotificationSender``
+ ``InMemoryPushNotificationConfigStore`` already complete on the server
(``agent/a2a_connector.py:369``). This module is the *receiver* that the
**user client** runs to catch pushes.

Server -> Receiver flow:
  Server's ``BasePushNotificationSender._dispatch_notification`` does
  ``POST {url} json=MessageToDict(to_stream_response(event))`` where ``event``
  is ``Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent`` as
  ``StreamResponse``. Receiver's job is to catch that POST, parse it, and
  *communicate it back to the client code* via ``wait_for_completion`` /
  queues / ``on_event`` callback, not to send anything itself.

Payload examples (camelCase, default ``MessageToDict`` without
``preserving_proto_field_name`` - exactly what server sends):

  {"task": {"id":"...","contextId":"...","status":{"state":"TASK_STATE_WORKING"}}}
  {"statusUpdate": {"taskId":"...","contextId":"...","status":{"state":"...","message":{...}}}}
  {"artifactUpdate": {"taskId":"...","contextId":"...","artifact":{...}}}

We also accept snake_case for robustness/manual tests:

  {"status_update": {"task_id": "...", ...}}

Design: separate webhook process/entity by requirement (URL must be reachable
by A2A server). asyncio-native (no ``threading.Thread``), ``port=0`` dynamic,
optional token auth (``X-A2A-Notification-Token`` - validated only if you set
``token``; by default open as A2A spec does not require auth, matching your
note "exposed for all requests"), per-task delivery to client via
``ParsedPushEvent`` + ``wait_for_completion``.

Usage (how unified ``A2ATaskService`` — ``tests/a2a_task_service.py`` -> ``client/a2a_client.py`` — will use it; see ``docs/push_receiver.md:49`` for full push pattern):

    from client.push_receiver import PushNotificationReceiver
    # from client.a2a_client import A2ATaskService  # future location of tests/a2a_task_service.py

    # client owns receiver lifetime; composition, not inheritance
    receiver = PushNotificationReceiver(host="127.0.0.1", port=0, on_event=lambda e: print(e.text))
    url = await receiver.start()  # http://127.0.0.1:XXXXX/webhook
    # give url to server: either ClientConfig(push_notification_config=TaskPushNotificationConfig(url=url))
    # or per-message: SendMessageRequest(configuration=SendMessageConfiguration(task_push_notification_config=...))
    # or after creation: raw_client.create_task_push_notification_config(TaskPushNotificationConfig(task_id=tid, url=url))
    # ... send via A2ATaskService to get task_id, then await ...
    # async for chunk in raw_client.send_message(req): tid = svc._extract_chunk_info(chunk)[1]
    ev = await receiver.wait_for_completion(tid, timeout=300)  # unblocks when server sent COMPLETED
    task = await svc.get_task(tid)  # fetch result
    await receiver.stop()

    # context manager
    async with PushNotificationReceiver(port=0) as rec:
        url = rec.url
        ...

Compatible with ``agent/a2a_connector.py:369`` sender.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from dataclasses import dataclass, field
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)


def _find_free_port(host: str = "127.0.0.1") -> int:
    """Find an OS-assigned free port.

    NOTE: TOCTOU race with uvicorn bind (`_find_free_port` -> `uvicorn.Config(port=...)`).
    OK for tests; for prod let OS assign `port=0` directly if uvicorn supported, or retry on `OSError: Address already in use`.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])  # type: ignore[no-any-return]


def _coalesce(*keys: str, d: dict[str, Any]) -> Any:
    """Return first present key (supports camelCase + snake_case)."""
    for k in keys:
        if k in d:
            return d[k]
    return None


@dataclass
class ParsedPushEvent:
    """Normalized view of a push payload."""

    raw: dict[str, Any]
    kind: str  # "task" | "status_update" | "artifact_update" | "message" | "unknown"
    task_id: Optional[str] = None
    context_id: Optional[str] = None
    state: Optional[str] = None
    text: Optional[str] = None


def parse_push_payload(payload: dict[str, Any]) -> ParsedPushEvent:
    """
    Parse a ``MessageToDict(StreamResponse)`` payload (camelCase or snake_case).

    Compatible with ``BasePushNotificationSender._dispatch_notification`` which does
    ``json=MessageToDict(to_stream_response(event))`` (camelCase) and also with
    manual ``preserving_proto_field_name=True`` payloads.
    """
    # Detect kind - check both naming conventions
    if "task" in payload:
        kind = "task"
        task_dict = payload["task"] or {}
        task_id = _coalesce("id", d=task_dict)
        context_id = _coalesce("contextId", "context_id", d=task_dict)
        state = _coalesce("state", d=(task_dict.get("status") or {}))
        # state may be dict with "state" field
        if isinstance(state, dict):
            state = state.get("state") or state.get("state")
        # fallback: try nested
        if not state:
            state = (task_dict.get("status") or {}).get("state")
        # extract text from history/message if present
        text = None
        # try history
        history = task_dict.get("history") or []
        for h in history:
            for part in h.get("parts") or []:
                if "text" in part:
                    text = part["text"]
                    break
            if text:
                break
        # fallback status message
        if not text:
            status = task_dict.get("status") or {}
            msg = status.get("message") or {}
            for part in msg.get("parts") or []:
                if "text" in part:
                    text = part["text"]
                    break
        # handle camelCase status
        if not context_id:
            context_id = task_dict.get("contextId")
        return ParsedPushEvent(
            raw=payload, kind=kind, task_id=task_id, context_id=context_id, state=state, text=text
        )

    # status_update / statusUpdate
    status_key = "status_update" if "status_update" in payload else ("statusUpdate" if "statusUpdate" in payload else None)
    if status_key:
        kind = "status_update"
        upd = payload[status_key] or {}
        task_id = _coalesce("taskId", "task_id", d=upd)
        context_id = _coalesce("contextId", "context_id", d=upd)
        status = upd.get("status") or {}
        state = status.get("state")
        text = None
        msg = status.get("message") or {}
        for part in msg.get("parts") or []:
            if "text" in part:
                text = part["text"]
                break
        return ParsedPushEvent(raw=payload, kind=kind, task_id=task_id, context_id=context_id, state=state, text=text)

    artifact_key = (
        "artifact_update" if "artifact_update" in payload else ("artifactUpdate" if "artifactUpdate" in payload else None)
    )
    if artifact_key:
        kind = "artifact_update"
        upd = payload[artifact_key] or {}
        task_id = _coalesce("taskId", "task_id", d=upd)
        context_id = _coalesce("contextId", "context_id", d=upd)
        artifact = upd.get("artifact") or {}
        text = None
        for part in artifact.get("parts") or []:
            if "text" in part:
                text = part["text"]
                break
        # state not in artifact, but keep None
        return ParsedPushEvent(raw=payload, kind=kind, task_id=task_id, context_id=context_id, state=None, text=text)

    # NOTE: `a2a/server/tasks/push_notification_sender.py:10`
    # `PushNotificationEvent = Task | TaskStatusUpdateEvent | TaskArtifactUpdateEvent`
    # never includes `Message`, so `message` branch is dead code for push path.
    # Kept only for debugging manual `StreamResponse.message` payloads; otherwise unknown.
    if "message" in payload:
        kind = "message"
        msg = payload["message"] or {}
        task_id = _coalesce("taskId", "task_id", d=msg)
        context_id = _coalesce("contextId", "context_id", d=msg)
        text = None
        for part in msg.get("parts") or []:
            if "text" in part:
                text = part["text"]
                break
        return ParsedPushEvent(raw=payload, kind=kind, task_id=task_id, context_id=context_id, state=None, text=text)

    return ParsedPushEvent(raw=payload, kind="unknown")


def _is_terminal_state(state: Optional[str]) -> bool:
    """True if task is in terminal state - mirrors SDK `TERMINAL_TASK_STATES`.

    SDK `a2a/server/agent_execution/active_task.py:83` =
    {COMPLETED, CANCELED, FAILED, REJECTED}. We add REJECTED which was missing
    before (critique #2) and accept both `TASK_STATE_X` and short `X`.
    """
    if not state:
        return False
    s = state.upper()
    return s in (
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_REJECTED",
        "COMPLETED",
        "FAILED",
        "CANCELED",
        "CANCELLED",
        "REJECTED",
    )


@dataclass
class PushNotificationReceiver:
    """
    Webhook receiver - the client-side counterpart to server's
    ``BasePushNotificationSender``. See module docstring.
    """
    """
    Asyncio-native push notification webhook server.

    Args:
        host: bind host (0.0.0.0 for external, 127.0.0.1 for local dev).
        port: bind port, 0 = OS-assigned free port.
        path: webhook path, must match URL given to server.
        token: if set, require ``X-A2A-Notification-Token`` header to equal this.
        external_host: host to advertise in URL (if bind is 0.0.0.0, external must be reachable).
        log_level: uvicorn log level.
    """

    host: str = "127.0.0.1"
    port: int = 0
    path: str = "/webhook"
    token: Optional[str] = None
    external_host: Optional[str] = None
    log_level: str = "warning"
    on_event: Optional[Any] = None  # Callable[[ParsedPushEvent], Any] - client callback for each push, called after parsing

    # runtime - not part of init comparison
    app: FastAPI = field(init=False, repr=False)
    _server: Optional[uvicorn.Server] = field(default=None, init=False, repr=False)
    _task: Optional[asyncio.Task[Any]] = field(default=None, init=False, repr=False)
    _actual_port: Optional[int] = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)

    # state - receiver keeps two views:
    # 1. `events`/`raw_events` = all pushes for debugging (includes WORKING etc.)
    # 2. `completed_task_ids` + `completed_events` = only terminal pushes - this is what client polls
    #    The point of the receiver is to tell client "task X completed, go fetch result"
    events: dict[str, list[ParsedPushEvent]] = field(default_factory=dict, init=False, repr=False)
    raw_events: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    completed_task_ids: set[str] = field(default_factory=set, init=False, repr=False)
    completed_events: dict[str, ParsedPushEvent] = field(default_factory=dict, init=False, repr=False)
    _completion_events: dict[str, asyncio.Event] = field(default_factory=dict, init=False, repr=False)
    _queues: dict[str, asyncio.Queue[ParsedPushEvent]] = field(default_factory=dict, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            self.path = "/" + self.path
        self.app = FastAPI(title="A2A Push Notification Receiver")
        self._setup_routes()
        # normalize external_host default
        if self.external_host is None:
            # if host is 0.0.0.0, external should be 127.0.0.1 for local testing
            if self.host == "0.0.0.0":
                self.external_host = "127.0.0.1"
            else:
                self.external_host = self.host

    def _setup_routes(self) -> None:
        @self.app.post(self.path)
        async def webhook(request: Request) -> Response:
            # Token check - only if receiver configured with token (sender must have same token in TaskPushNotificationConfig.token)
            # By default receiver is open (matches A2A spec default, as you noted). Set token only if you need to reject spoofed pushes.
            if self.token is not None:
                hdr = request.headers.get("X-A2A-Notification-Token") or request.headers.get("x-a2a-notification-token")
                if hdr != self.token:
                    logger.warning("Push webhook token mismatch: got %r", hdr)
                    return JSONResponse({"error": "invalid token"}, status_code=401)

            try:
                payload = await request.json()
            except Exception as e:
                logger.warning("Push webhook invalid JSON: %s", e)
                return JSONResponse({"error": "invalid json"}, status_code=400)

            if not isinstance(payload, dict):
                return JSONResponse({"error": "payload must be object"}, status_code=400)

            parsed = parse_push_payload(payload)
            logger.info(
                "Push received kind=%s task_id=%s state=%s text=%.120r",
                parsed.kind,
                parsed.task_id,
                parsed.state,
                parsed.text or "",
            )

            # Always keep raw for debugging, but client-facing "completed" store
            # is only terminal states. Sender *does* send every event (Task/Status/Artifact
            # per `default_request_handler.py:373,444`), but receiver's contract to client
            # is "task X completed" so client can `get_task` the result. See tests.
            is_terminal = _is_terminal_state(parsed.state)
            # NOTE: `await queue.put` inside `async with _lock` is low-risk here (unbounded queue,
            # never blocks) but would deadlock if queue were bounded or waiter held lock.
            # We capture queue ref under lock, then `put` outside to avoid anti-pattern.
            async with self._lock:
                self.raw_events.append(payload)
                tid = parsed.task_id or "_unknown"
                self.events.setdefault(tid, []).append(parsed)
                if tid not in self._queues:
                    self._queues[tid] = asyncio.Queue()
                queue_ref = self._queues[tid]
                if tid not in self._completion_events:
                    self._completion_events[tid] = asyncio.Event()
                completion_event_ref = self._completion_events[tid]
                if is_terminal:
                    self.completed_task_ids.add(tid)
                    self.completed_events[tid] = parsed
            # outside lock
            await queue_ref.put(parsed)
            if is_terminal:
                completion_event_ref.set()

            # Communicate back to client *only on terminal* - this is the signal
            # "task completed, go fetch result". If you need per-status streaming,
            # set `notify_all=True` (not exposed here to keep minimal receiver).
            if is_terminal and self.on_event is not None:
                try:
                    res = self.on_event(parsed)
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    logger.exception("on_event callback failed")

            # Server only checks raise_for_status, any 2xx is success.
            # We return simple JSON, not JSONRPC.
            return JSONResponse({"status": "ok"})

        @self.app.get("/health")
        async def health() -> dict[str, str]:
            return {"status": "ok", "events": str(len(self.raw_events))}

        @self.app.get("/events")
        async def list_events(task_id: Optional[str] = None) -> dict[str, Any]:
            if task_id:
                evs = self.events.get(task_id, [])
                return {"task_id": task_id, "count": len(evs), "events": [e.raw for e in evs]}
            return {"total": len(self.raw_events), "by_task": {k: len(v) for k, v in self.events.items()}}

    @property
    def url(self) -> str:
        """External URL to give to ``TaskPushNotificationConfig``."""
        if self._actual_port is None:
            # not yet started - predict (if port==0 we haven't bound yet)
            port = self.port if self.port != 0 else 0
            return f"http://{self.external_host}:{port}{self.path}"
        return f"http://{self.external_host}:{self._actual_port}{self.path}"

    @property
    def bound_port(self) -> Optional[int]:
        return self._actual_port

    async def start(self) -> str:
        """Start the webhook server, return external URL.

        Idempotent - if already started returns same URL.
        """
        if self._started:
            return self.url

        # resolve port 0 -> free port
        if self.port == 0:
            # use external_host for port discovery if host is 0.0.0.0
            host_for_probe = "127.0.0.1" if self.host == "0.0.0.0" else self.host
            self._actual_port = _find_free_port(host_for_probe)
        else:
            self._actual_port = self.port

        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self._actual_port,  # type: ignore[arg-type]
            log_level=self.log_level,
            lifespan="off",
        )
        self._server = uvicorn.Server(config)

        # uvicorn.Server.serve() blocks - run as task
        self._task = asyncio.create_task(self._server.serve())
        # wait until started (or failed)
        for _ in range(50):  # up to 5s
            if getattr(self._server, "started", False):
                break
            if self._task.done():
                # server failed to start (port in use etc)
                exc = self._task.exception()
                raise RuntimeError(f"Push server failed to start: {exc}") from exc
            await asyncio.sleep(0.1)
        else:
            logger.warning("Push server start timeout, still proceeding")

        # extra small delay to ensure socket bound
        await asyncio.sleep(0.2)
        self._started = True
        logger.info("PushNotificationReceiver started at %s (bound %s:%s)", self.url, self.host, self._actual_port)
        return self.url

    async def stop(self) -> None:
        """Stop the server gracefully."""
        if not self._started or self._server is None:
            return
        self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
                try:
                    await self._task
                except asyncio.CancelledError:
                    pass
            except Exception:
                pass
        self._started = False
        logger.info("PushNotificationReceiver stopped")

    async def __aenter__(self) -> "PushNotificationReceiver":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()

    # ---------- helpers for client/tests ----------

    def get_events(self, task_id: Optional[str] = None) -> list[ParsedPushEvent]:
        """Return parsed events, optionally filtered by task_id."""
        if task_id is None:
            # flatten
            out: list[ParsedPushEvent] = []
            for lst in self.events.values():
                out.extend(lst)
            return out
        return list(self.events.get(task_id, []))

    def get_last_text(self, task_id: str) -> Optional[str]:
        """Return last text for task_id."""
        evs = self.events.get(task_id, [])
        for e in reversed(evs):
            if e.text:
                return e.text
        return None

    def get_completed_task_ids(self) -> set[str]:
        """Return set of task_ids that have received a terminal push."""
        return set(self.completed_task_ids)

    def is_completed(self, task_id: str) -> bool:
        return task_id in self.completed_task_ids

    async def wait_for_completion(
        self,
        task_id: str,
        timeout: float = 300.0,
    ) -> ParsedPushEvent:
        """
        Wait until terminal state (COMPLETED/FAILED/CANCELED) for ``task_id``.
        Returns the terminal Completed event (so client can then `get_task`).
        """
        # fast path - already completed
        if task_id in self.completed_events:
            return self.completed_events[task_id]
        ev = self._completion_events.get(task_id)
        if ev is not None and ev.is_set() and task_id in self.completed_events:
            return self.completed_events[task_id]

        # ensure event exists
        if task_id not in self._completion_events:
            self._completion_events[task_id] = asyncio.Event()

        try:
            await asyncio.wait_for(self._completion_events[task_id].wait(), timeout=timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Timeout waiting for push completion for task_id={task_id}") from None
        return self.completed_events[task_id]

    async def wait_for_event(
        self,
        task_id: Optional[str] = None,
        kind: Optional[str] = None,
        timeout: float = 30.0,
    ) -> ParsedPushEvent:
        """
        Wait for next event matching ``task_id``/``kind``. Useful for tests.
        If ``task_id`` is None, waits for any task.
        """
        # if already have matching event, return immediately? No - wait for next
        queue_key = task_id or "_any"
        # For generic wait, we need to observe all queues - simpler: poll self.events
        # For task-specific, use queue
        if task_id and task_id in self._queues:
            try:
                # if kind filter, may need to loop
                while True:
                    ev = await asyncio.wait_for(self._queues[task_id].get(), timeout=timeout)
                    if kind is None or ev.kind == kind:
                        return ev
            except asyncio.TimeoutError:
                raise TimeoutError(f"wait_for_event timeout task_id={task_id} kind={kind}") from None
        # fallback polling for _any or unknown queue
        deadline = asyncio.get_event_loop().time() + timeout
        seen = len(self.get_events(task_id))
        while True:
            await asyncio.sleep(0.05)
            evs = self.get_events(task_id)
            if len(evs) > seen:
                for e in evs[seen:]:
                    if kind is None or e.kind == kind:
                        return e
                seen = len(evs)
            if asyncio.get_event_loop().time() > deadline:
                raise TimeoutError(f"wait_for_event timeout (any) kind={kind}")

    def clear(self) -> None:
        """Clear stored events (useful between tests).

        NOTE: pending `wait_for_completion` waiters will timeout (no explicit cancel).
        If you need immediate wake, create new receiver instance per test or call
        `clear` only when no waiters are pending.
        """
        self.events.clear()
        self.raw_events.clear()
        self.completed_task_ids.clear()
        self.completed_events.clear()
        # wake pending waiters so they don't hang until timeout - they will see empty and re-wait, so just clear events
        # We deliberately do not cancel waiters; they will timeout and raise TimeoutError.
        self._completion_events.clear()
        # drain queues
        for q in self._queues.values():
            while not q.empty():
                try:
                    q.get_nowait()
                except Exception:
                    break
        self._queues.clear()



