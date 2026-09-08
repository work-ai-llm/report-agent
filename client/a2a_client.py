"""
client/a2a_client.py — Unified A2A client.

Class: A2AClient (wraps a2a SDK Client from create_client)
  Methods:
    create(server_url, streaming, httpx_client, polling) -> A2AClient  # factory, mirrors 3_a2a_testing.py:254-260
    close()                                                    # closes client + owned httpx
    send_message(text, context_id) -> (task_id, context_id, text)             # 3.1.1 non-streaming, first chunk
    send_streaming_message(text, context_id) -> (task_id, context_id, artifact) # 3.1.2 streaming, full loop
    subscribe_to_task(task_id) -> (task_id, context_id, artifact)            # 3.1.6
    get_task(task_id) -> Task                                                 # 3.1.3
    list_tasks(context_id, status, page_size, ...) -> ListTasksResponse      # 3.1.4
    cancel_task(task_id) -> Task                                              # 3.1.5
    poll_until_complete(task_id) -> Task                                      # helper
  Helpers:
    _create_send_request(text, context_id) -> (SendMessageRequest, context_id)
    _extract_chunk_info(chunk) -> (text, task_id, context_id, chunk_type)    # handles Task|Status|Artifact
    _process_stream(stream) -> (task_id, context_id, artifact)                # consumes streaming iterator
    _is_streaming() -> bool                                                   # effective streaming flag
"""

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
        # Reference: SDK client holds _config.streaming and _card.capabilities.streaming
        # High-level: explicit `streaming` overrides auto-detect; None means infer from the wrapped SDK client.
        self.client = client
        if streaming is not None:
            self._explicit_streaming: Optional[bool] = bool(streaming)
        else:
            self._explicit_streaming = None
        # Stashed by create() for close()
        self._httpx_client: Optional[httpx.AsyncClient] = None  # type: ignore[assignment]
        self._owns_httpx = False
        self._server_url: Optional[str] = None

    # ------------------------------------------------------------------
    # Factory helpers
    # ------------------------------------------------------------------
    def _is_streaming(self) -> bool:
        """Return whether this wrapper should behave as streaming.

        Reference: checks explicit flag first, else SDK `client._config.streaming`
        and server card `client._card.capabilities.streaming` (agent/a2a_connector.py:357 advertises True).
        High-level: does the server + client actually support streaming right now?
        """
        # High-level: if caller forced streaming via A2AClient(client, streaming=True/False), trust that
        if self._explicit_streaming is not None:
            return bool(self._explicit_streaming)
        # High-level: otherwise ask the SDK client what it was configured with
        try:
            underlying_client_config = getattr(self.client, "_config", None)
            is_client_streaming_enabled = bool(underlying_client_config and getattr(underlying_client_config, "streaming", True))
            # High-level: also check what the server says it can do (agent card)
            agent_card = getattr(self.client, "_card", None) or getattr(self.client, "card", None)
            if agent_card is not None and hasattr(agent_card, "capabilities"):
                is_server_streaming_enabled = bool(getattr(agent_card.capabilities, "streaming", True))
                # High-level: both must be True to actually stream
                return bool(is_client_streaming_enabled and is_server_streaming_enabled)
            return is_client_streaming_enabled
        except Exception:
            # High-level: if anything missing, assume streaming (report agent always streams)
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
        """Create resolver+client and wrap.

        Reference: mirrors 3_a2a_testing.py:254-260 (A2ACardResolver -> create_client with ClientConfig).
        High-level: one-liner to get a ready A2AClient; streaming flag decides SendMessage vs SendStreamingMessage.
        """
        # Reference: SDK ClientConfig fields: streaming, polling (return_immediately), httpx_client
        # High-level: build the SDK client the way the acceptance tests did, then wrap it
        owns_httpx_client = False
        if httpx_client is None:
            httpx_client = httpx.AsyncClient(timeout=300.0)
            owns_httpx_client = True
        card_resolver = A2ACardResolver(httpx_client=httpx_client, base_url=server_url)
        agent_card = await card_resolver.get_agent_card()
        sdk_client_config = ClientConfig(streaming=streaming, polling=polling, httpx_client=httpx_client)
        sdk_client = await create_client(agent_card, client_config=sdk_client_config)
        wrapped_client = cls(sdk_client, streaming=streaming)
        wrapped_client._httpx_client = httpx_client  # type: ignore[attr-defined]
        wrapped_client._owns_httpx = owns_httpx_client  # type: ignore[attr-defined]
        wrapped_client._server_url = server_url  # type: ignore[attr-defined]
        return wrapped_client

    async def close(self):
        """Close underlying SDK client + owned httpx."""
        # Reference: SDK Client.close() + httpx.AsyncClient.aclose()
        # High-level: clean up network resources; safe to call twice
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
        """Build a SendMessageRequest from text.

        Reference: a2a.types.a2a_pb2.Message/Part/Role + uuid for message_id/context_id
        High-level: turns your string prompt into the protobuf the SDK wants, creating a new context if needed
        """
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
        """Parse a StreamResponse chunk.

        Reference: google.protobuf.json_format.MessageToDict(preserving_proto_field_name=True) on StreamResponse,
        then inspects task / status_update / artifact_update branches (a2a spec Task|TaskStatusUpdateEvent|TaskArtifactUpdateEvent)
        High-level: whatever the server streamed, give back (human text, task_id, context_id, kind) so callers don't parse proto
        """
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
            context_id = task_dict.get('context_id') or task_dict.get('contextId')
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
        task_id = getattr(chunk, 'task_id', None) or chunk_dict.get('task_id') or chunk_dict.get('taskId')
        context_id = getattr(chunk, 'context_id', None) or chunk_dict.get('context_id') or chunk_dict.get('contextId')

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
        """Consume a streaming iterator and aggregate final artifact.

        Reference: iterates async `stream` of StreamResponse, calls _extract_chunk_info per chunk
        High-level: used by streaming calls to collect all server updates and return the report artifact
        """
        final_artifact = ""
        task_id = None
        context_id = None
        chunk_count = 0

        async for chunk in stream:
            chunk_count += 1
            text, extracted_task_id, extracted_context_id, chunk_kind = self._extract_chunk_info(chunk)

            if extracted_task_id:
                task_id = extracted_task_id
            if extracted_context_id:
                context_id = extracted_context_id

            if text:
                print(f"{label}{text}")

            if chunk_kind == "ARTIFACT":
                final_artifact = text

        print(f"{label}--- Stream finished. Total chunks: {chunk_count} ---")
        return task_id, context_id, final_artifact

    # ==========================================
    # PUBLIC API (The "Service" Methods)
    # ==========================================
    async def send_message(self, text, context_id=None, label=""):
        """3.1.1 SendMessage — send-and-forget (non-streaming).

        Reference: SDK transport `message/send` via `client.send_message` when streaming False (a2a/client/base_client.py:send_message)
        High-level: fire the prompt and get back the first task immediately (task_id needed for resubscribe/poll); not the full streamed report
        """
        request, generated_context_id = self._create_send_request(text, context_id)
        mode_label = "STREAMING" if self._is_streaming() else "NON-STREAMING"
        print(f"\n{label}[CLIENT SENDING - {mode_label}]: {text}")
        # Reference: SDK still yields StreamResponse even for non-streaming (one item); we take only first
        # High-level: single response is enough to learn task_id/context_id for later get/subscribe
        async for chunk in self.client.send_message(request):
            extracted_text, extracted_task_id, extracted_context_id, chunk_kind = self._extract_chunk_info(chunk)
            if extracted_text:
                print(f"{label}{extracted_text}")
            return extracted_task_id or None, extracted_context_id or generated_context_id, extracted_text
        return None, generated_context_id, None

    async def send_streaming_message(self, text, context_id=None, label=""):
        """3.1.2 SendStreamingMessage — streams to completion.

        Reference: SDK transport `message/stream` via `client.send_message` when streaming True (a2a/client/base_client.py:send_message -> send_message_streaming)
        High-level: fire prompt and stream all status + artifact chunks until the report is done; this is what 3_a2a_testing.py:107 did
        """
        request, generated_context_id = self._create_send_request(text, context_id)
        print(f"\n{label}[CLIENT SENDING - STREAMING]: {text}")

        stream = self.client.send_message(request)
        task_id, context_id, artifact = await self._process_stream(stream, label=f"{label}  ")
        return task_id, context_id, artifact

    async def subscribe_to_task(self, task_id, label=""):
        """3.1.6 SubscribeToTask — resubscribe to task event stream.

        Reference: SDK `client.subscribe(SubscribeToTaskRequest)` (a2a/client/base_client.py:subscribe)
        High-level: after a drop, re-attach to the same task and stream its remaining updates
        """
        print(f"\n{label}[SUBSCRIBING TO TASK]: {task_id}")

        # FIX: BaseClient requires a Protobuf request object and uses .subscribe()
        req = SubscribeToTaskRequest(id=task_id)
        stream = self.client.subscribe(req)

        task_id, context_id, artifact = await self._process_stream(stream, label=f"{label}  ")
        return task_id, context_id, artifact

    async def get_task(self, task_id, label=""):
        """3.1.3 GetTask.

        Reference: SDK `client.get_task(GetTaskRequest)` (a2a/client/base_client.py:get_task)
        High-level: fetch current task state/history on demand (polling)
        """
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
        """3.1.4 ListTasks.

        Reference: SDK `client.list_tasks(ListTasksRequest)` fields from a2a/types/a2a_pb2.py
        High-level: list tasks for this agent, optionally filtered by context/status with paging
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
        """3.1.5 CancelTask.

        Reference: SDK `client.cancel_task(CancelTaskRequest)` (a2a/client/base_client.py:cancel_task)
        High-level: ask server to cancel a running task; server enqueues TASK_STATE_CANCELED via agent/a2a_connector.py:281
        """
        print(f"\n{label}[CANCELLING TASK]: {task_id}")

        # FIX: BaseClient requires a Protobuf request object
        req = CancelTaskRequest(id=task_id)
        task_data = await self.client.cancel_task(req)
        state = task_data.status.state
        print(f"{label}  -> State after cancel: {state}")
        return task_data

    async def poll_until_complete(self, task_id, interval=5, max_attempts=10, label=""):
        """Helper: poll get_task until COMPLETED.

        Reference: loops get_task + asyncio.sleep
        High-level: when you used non-streaming SendMessage, wait until the task finishes without streaming
        """
        for attempt in range(1, max_attempts + 1):
            task_data = await self.get_task(task_id, label=f"{label}Poll {attempt}/{max_attempts}: ")
            if task_data.status.state == TaskState.TASK_STATE_COMPLETED:
                return task_data
            if attempt < max_attempts:
                print(f"{label}  -> Waiting {interval}s for next check...")
                await asyncio.sleep(interval)
        return task_data
