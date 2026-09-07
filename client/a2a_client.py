# a2a_client.py
#
# UNIFIED A2A client (PR-B). Moved from tests/a2a_task_service.py -> client/a2a_client.py
# per easy-fix: rename + add send_message / send_streaming_message split + list_tasks.
#
# Streaming vs non-streaming (fixed from alias):
# - SDK `a2a.client.Client.send_message` in `a2a/client/base_client.py:send_message`
#   multiplexes JSON-RPC `message/send` vs `message/stream` via
#   `ClientConfig(streaming)` + `AgentCard.capabilities.streaming`. If either False,
#   `transport.send_message` (single `SendMessageResponse`) -> one `StreamResponse`.
#   If both True, `transport.send_message_streaming` -> many `StreamResponse`s.
# - This wrapper now respects `self._streaming` / `client._config.streaming`
#   (set via `A2AClient.create(streaming=...)` or `__init__(client, streaming=...)`).
#   `send_message` (3.1.1) is true send-and-forget: consumes only first chunk (single
#   Task|Message) - not `_process_stream`. `send_streaming_message` (3.1.2) streams
#   to completion via `_process_stream` (former `send_message` behaviour).
#   See `tests/acceptance/3_a2a_testing.py:259` for `ClientConfig(streaming=True)` origin.
#
# Source: tests/a2a_task_service.py (stash@{0} A2ATaskService) + 3_a2a_testing.py:254-260
# client init pattern. Faithfully preserved; added list_tasks + push-free.

import uuid
import asyncio
from typing import Optional

import httpx
from google.protobuf.json_format import MessageToDict

from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.types.a2a_pb2 import (
    Part,
    Role,
    Message,
    SendMessageRequest,
    GetTaskRequest,
    CancelTaskRequest,
    SubscribeToTaskRequest,
    ListTasksRequest,
    ListTasksResponse,
    TaskState,
)


class A2AClient:
    """Full task-lifecycle A2A client.

    Wraps a lower-level A2A client (from create_client) and provides the full
    lifecycle: send / subscribe / get / cancel / poll + list. Kept close to the SDK;
    this is the core the acceptance test (PR-a2a-acceptance) drives against.

    Client creation (mirrors 3_a2a_testing.py:254-260):
        httpx_client = httpx.AsyncClient(timeout=300.0)
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=server_url)
        card = await resolver.get_agent_card()
        client = await create_client(card, ClientConfig(streaming=True, httpx_client=httpx_client))
        svc = A2AClient(client, streaming=True)
    or via helper:
        svc = await A2AClient.create(server_url, streaming=True)
    Streaming flag controls `send_message` (3.1.1 single) vs `send_streaming_message` (3.1.2 streamed).
    """

    def __init__(self, client, streaming: Optional[bool] = None):
        self.client = client
        # Explicit streaming param overrides auto-detect; None -> infer from client._config / _card
        if streaming is not None:
            self._streaming: Optional[bool] = bool(streaming)
        else:
            self._streaming = None
        # compat for old create() stashed attrs
        self._httpx_client: Optional[httpx.AsyncClient] = None  # type: ignore[assignment]
        self._owns_httpx = False
        self._server_url: Optional[str] = None

    # ------------------------------------------------------------------
    # Factory helpers (the "actual client" you noted was missing)
    # ------------------------------------------------------------------
    def _is_streaming(self) -> bool:
        """Resolve effective streaming flag.

        Priority: explicit `self._streaming` (via __init__ or create) > client._config.streaming
        && client._card.capabilities.streaming > default True. Lets caller create
        one client and toggle per-call via `A2AClient(client, streaming=False)`.
        """
        if self._streaming is not None:
            return bool(self._streaming)
        try:
            cfg_stream = bool(getattr(self.client, "_config", None) and getattr(self.client._config, "streaming", True))
            # card may be _card or card; capabilities.streaming is server advert (True on report agent agent/a2a_connector.py:357)
            card = getattr(self.client, "_card", None) or getattr(self.client, "card", None)
            if card is not None and hasattr(card, "capabilities"):
                return bool(cfg_stream and getattr(card.capabilities, "streaming", True))
            return cfg_stream
        except Exception:
            return True

    @classmethod
    async def create(
        cls,
        server_url: str,
        *,
        streaming: bool = True,
        httpx_client: Optional[httpx.AsyncClient] = None,
        polling: bool = False,
    ) -> "A2AClient":
        """Create resolver+client and wrap. Mirrors 3_a2a_testing.py:254-260.

        Args:
            server_url: e.g. http://192.168.0.107:9999
            streaming: maps to ClientConfig(streaming). True -> streaming path
                       (a2a/client/base_client.py:streaming branch), False -> non-streaming.
            httpx_client: reuse or None -> new AsyncClient(timeout=300)
            polling: ClientConfig.polling (return_immediately)
        """
        close_ours = False
        if httpx_client is None:
            httpx_client = httpx.AsyncClient(timeout=300.0)
            close_ours = True  # caller still owns via svc, but we keep ref
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=server_url)
        card = await resolver.get_agent_card()
        config = ClientConfig(streaming=streaming, polling=polling, httpx_client=httpx_client)
        raw = await create_client(card, client_config=config)
        # stash httpx_client for close() and wrap with explicit streaming
        inst = cls(raw, streaming=streaming)
        inst._httpx_client = httpx_client  # type: ignore[attr-defined]
        inst._owns_httpx = close_ours  # type: ignore[attr-defined]
        inst._server_url = server_url  # type: ignore[attr-defined]
        return inst

    async def close(self):
        """Close underlying client + httpx if we own it."""
        try:
            if hasattr(self.client, "close"):
                await self.client.close()
        finally:
            if getattr(self, "_httpx_client", None) is not None and getattr(self, "_owns_httpx", False):
                try:
                    await self._httpx_client.aclose()  # type: ignore[attr-defined]
                except Exception:
                    pass

    # ==========================================
    # INTERNAL HELPERS (The "Plumbing")
    # ==========================================
    def _create_send_request(self, text, context_id=None):
        """Encapsulates the Protobuf message creation."""
        if not context_id:
            context_id = str(uuid.uuid4())
            print(f">>> CLIENT GENERATED CONTEXT ID: {context_id}")

        part = Part(text=text)
        msg_kwargs = {
            'role': Role.ROLE_USER,
            'parts': [part],
            'message_id': str(uuid.uuid4()),
            'context_id': context_id,
        }
        message = Message(**msg_kwargs)
        return SendMessageRequest(message=message), context_id

    def _extract_chunk_info(self, chunk):
        """Takes raw Protobuf chunk. Returns: (text_content, task_id, context_id, chunk_type)"""
        chunk_dict = MessageToDict(chunk, preserving_proto_field_name=True)
        text_content = None
        chunk_type = None
        task_id = None
        context_id = None

        # 1. Handle the INITIAL TASK chunk (IDs are nested inside 'task')
        if "task" in chunk_dict:
            chunk_type = "INITIAL_TASK"
            task_dict = chunk_dict['task']
            task_id = task_dict.get('id')       # <--- FIX: Correctly extract nested ID
            context_id = task_dict.get('contextId')
            status = task_dict.get("status", {}).get("state")
            text_content = f"[TASK STATE]: {status}\nTask Id: {task_id}"

            # Restore history extraction
            history_list = task_dict.get("history", [])
            for history_item in history_list:
                parts = history_item.get("parts", [])
                for part in parts:
                    if "text" in part:
                        text_content += f"\nText: {part['text']}"
                        break
            return text_content, task_id, context_id, chunk_type

        # 2. Handle Updates (IDs are at the root)
        task_id = getattr(chunk, 'task_id', None) or chunk_dict.get('task_id')
        context_id = getattr(chunk, 'context_id', None) or chunk_dict.get('context_id')

        if "status_update" in chunk_dict:
            chunk_type = "STATUS"
            message = chunk_dict["status_update"].get("status", {}).get("message", {})
            for part in message.get("parts", []):
                if "text" in part:
                    text_content = f"[STATUS]: {part['text']}"
                    break

        elif "artifact_update" in chunk_dict:
            chunk_type = "ARTIFACT"
            artifact = chunk_dict["artifact_update"].get("artifact", {})
            for part in artifact.get("parts", []):
                if "text" in part:
                    text_content = f"[ARTIFACT]: {part['text']}"
                    break

        return text_content, task_id, context_id, chunk_type

    async def _process_stream(self, stream, label=""):
        """Centralized pipeline to consume a stream, extract data, and print it."""
        final_artifact = ""
        task_id = None
        context_id = None
        chunk_count = 0

        async for chunk in stream:
            chunk_count += 1
            text, tid, cid, ctype = self._extract_chunk_info(chunk)

            if tid:
                task_id = tid
            if cid:
                context_id = cid

            if text:
                print(f"{label}{text}")

            if ctype == "ARTIFACT":
                final_artifact = text

        print(f"{label}--- Stream finished. Total chunks: {chunk_count} ---")
        return task_id, context_id, final_artifact

    # ==========================================
    # PUBLIC API (The "Service" Methods)
    # ==========================================
    async def send_message(self, text, context_id=None, label=""):
        """3.1.1 SendMessage — send-and-forget (non-streaming).

        Single `Task|Message` return. Uses `self.client.send_message(request)` which
        when `ClientConfig(streaming=False)` or server `capabilities.streaming=False`
        calls `transport.send_message` (one `StreamResponse` via
        `a2a/client/base_client.py:send_message`). This method consumes ONLY the
        first chunk and returns `(task_id, context_id, text)` — not `_process_stream`.
        Respects `self._streaming` / `client._config.streaming`; if streaming True,
        caller should use `send_streaming_message` for full stream. Keeps
        3.1.1 vs 3.1.2 spec separation (previously alias, now actual non-streaming).
        """
        request, ctx_id = self._create_send_request(text, context_id)
        mode = "STREAMING" if self._is_streaming() else "NON-STREAMING"
        print(f"\n{label}[CLIENT SENDING - {mode}]: {text}")
        # Non-streaming: take first chunk only (send-and-forget)
        async for chunk in self.client.send_message(request):
            text_content, tid, cid, ctype = self._extract_chunk_info(chunk)
            if text_content:
                print(f"{label}{text_content}")
            # effective IDs fall back to request context if chunk missing
            return tid or None, cid or ctx_id, text_content
        return None, ctx_id, None

    async def send_streaming_message(self, text, context_id=None, label=""):
        """3.1.2 SendStreamingMessage — streams to completion.

        Uses `self.client.send_message(request)` which, when `ClientConfig(streaming=True)`
        and server streaming True, calls `transport.send_message_streaming`
        (`SendStreamingMessage` JSON-RPC) and yields many chunks. Consumed via
        `_process_stream` to surface status/artifact and return final artifact.
        This is the behaviour previously exposed as `send_message` (3_a2a_testing.py:107).
        """
        request, ctx_id = self._create_send_request(text, context_id)
        print(f"\n{label}[CLIENT SENDING - STREAMING]: {text}")

        stream = self.client.send_message(request)
        tid, cid, artifact = await self._process_stream(stream, label=f"{label}  ")
        return tid, cid, artifact

    async def subscribe_to_task(self, task_id, label=""):
        """3.1.6 SubscribeToTask — resubscribe to task event stream."""
        print(f"\n{label}[SUBSCRIBING TO TASK]: {task_id}")

        # FIX: BaseClient requires a Protobuf request object and uses .subscribe()
        req = SubscribeToTaskRequest(id=task_id)
        stream = self.client.subscribe(req)

        tid, cid, artifact = await self._process_stream(stream, label=f"{label}  ")
        return tid, cid, artifact

    async def get_task(self, task_id, label=""):
        """3.1.3 GetTask"""
        print(f"\n{label}[GETTING TASK]: {task_id}")

        # FIX: BaseClient requires a Protobuf request object
        req = GetTaskRequest(id=task_id)
        task_data = await self.client.get_task(req)
        state = task_data.status.state
        print(f"{label}  -> Current State: {state}")
        return task_data

    async def list_tasks(
        self,
        *,
        context_id: Optional[str] = None,
        status: Optional[int] = None,
        page_size: Optional[int] = None,
        page_token: Optional[str] = None,
        include_artifacts: bool = False,
        history_length: Optional[int] = None,
        label: str = "",
    ) -> ListTasksResponse:
        """3.1.4 ListTasks — wrapper over `Client.list_tasks`.

        Mirrors `a2a/types/a2a_pb2.py:ListTasksRequest` fields:
          context_id, status (TaskState enum), page_size, page_token,
          history_length, include_artifacts (bool). Tenant/history filters omitted.

        Example:
            resp = await svc.list_tasks(context_id=ctx, page_size=10, include_artifacts=True)
            for t in resp.tasks: print(t.id, t.status.state)
        """
        if label:
            print(f"\n{label}[LISTING TASKS] ctx={context_id} status={status} page_size={page_size}")
        req_kwargs = {}
        if context_id is not None:
            req_kwargs["context_id"] = context_id
        if status is not None:
            req_kwargs["status"] = status
        if page_size is not None:
            req_kwargs["page_size"] = page_size
        if page_token is not None:
            req_kwargs["page_token"] = page_token
        if history_length is not None:
            req_kwargs["history_length"] = history_length
        # status_timestamp_after omitted (no filter)
        req_kwargs["include_artifacts"] = include_artifacts
        req = ListTasksRequest(**req_kwargs)
        resp: ListTasksResponse = await self.client.list_tasks(req)
        if label:
            print(f"{label}  -> got {len(resp.tasks)} tasks (next_token={resp.next_page_token!r})")
        return resp

    async def cancel_task(self, task_id, label=""):
        """3.1.5 CancelTask"""
        print(f"\n{label}[CANCELLING TASK]: {task_id}")

        # FIX: BaseClient requires a Protobuf request object
        req = CancelTaskRequest(id=task_id)
        task_data = await self.client.cancel_task(req)
        state = task_data.status.state
        print(f"{label}  -> State after cancel: {state}")
        return task_data

    async def poll_until_complete(self, task_id, interval=5, max_attempts=10, label=""):
        """Polls get_task repeatedly until COMPLETED or max attempts reached."""
        for attempt in range(1, max_attempts + 1):
            task_data = await self.get_task(task_id, label=f"{label}Poll {attempt}/{max_attempts}: ")
            if task_data.status.state == TaskState.TASK_STATE_COMPLETED:
                return task_data
            if attempt < max_attempts:
                print(f"{label}  -> Waiting {interval}s for next check...")
                await asyncio.sleep(interval)
        return task_data


# Removed redundant alias A2ATaskService — now only A2AClient (per cleanup).
# For external compat, import via: from client.a2a_client import A2AClient
