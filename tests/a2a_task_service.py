# a2a_task_service.py
#
# UNIFIED A2A client (PR-B gather).
#
# Source: extracted from stash@{0} `A2ATaskService` (the "proper single client"
# lifecycle class). Faithfully preserved; NOT yet unified with the stream/push
# modes from stash@{2} (client_utils.py / client_streaming.py / client_push.py).
# See docs/PR-a2a-client.md for the target architecture and what is left to do.
import uuid
import asyncio
from google.protobuf.json_format import MessageToDict
from a2a.types.a2a_pb2 import (
    Part, Role, Message, SendMessageRequest,
    GetTaskRequest, CancelTaskRequest, SubscribeToTaskRequest, TaskState,
)


class A2ATaskService:
    """Full task-lifecycle A2A client.

    Wraps a lower-level A2A client (from create_client) and provides the full
    lifecycle: send / subscribe / get / cancel / poll. Kept close to the SDK;
    this is the core the acceptance test (PR-a2a-acceptance) drives against.
    """

    def __init__(self, client):
        self.client = client

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
            task_id = task_dict.get('id')
            context_id = task_dict.get('contextId')
            status = task_dict.get("status", {}).get("state")
            text_content = f"[TASK STATE]: {status}\nTask Id: {task_id}"

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
        """Sends a new message and streams the response to completion."""
        request, ctx_id = self._create_send_request(text, context_id)
        print(f"\n{label}[CLIENT SENDING]: {text}")

        stream = self.client.send_message(request)
        tid, cid, artifact = await self._process_stream(stream, label=f"{label}  ")
        return tid, cid, artifact

    async def subscribe_to_task(self, task_id, label=""):
        print(f"\n{label}[SUBSCRIBING TO TASK]: {task_id}")

        # BaseClient requires a Protobuf request object and uses .subscribe()
        req = SubscribeToTaskRequest(id=task_id)
        stream = self.client.subscribe(req)

        tid, cid, artifact = await self._process_stream(stream, label=f"{label}  ")
        return tid, cid, artifact

    async def get_task(self, task_id, label=""):
        print(f"\n{label}[GETTING TASK]: {task_id}")

        # BaseClient requires a Protobuf request object
        req = GetTaskRequest(id=task_id)
        task_data = await self.client.get_task(req)
        state = task_data.status.state
        print(f"{label}  -> Current State: {state}")
        return task_data

    async def cancel_task(self, task_id, label=""):
        print(f"\n{label}[CANCELLING TASK]: {task_id}")

        # BaseClient requires a Protobuf request object
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
