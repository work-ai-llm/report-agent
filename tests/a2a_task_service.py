# a2a_task_service.py
# Compatibility shim — moved to client/a2a_client.py (rename per easy-fix).
# Keep import path stable for pr-a2a-acceptance; new code should import from client.a2a_client.
from client.a2a_client import A2ATaskService, A2AClient  # noqa: F401

# Re-export legacy helpers for direct importers (keeps file import-safe)
import uuid  # noqa: F401
import asyncio  # noqa: F401
from google.protobuf.json_format import MessageToDict  # noqa: F401
from a2a.types.a2a_pb2 import (  # noqa: F401
    Part,
    Role,
    Message,
    SendMessageRequest,
    GetTaskRequest,
    CancelTaskRequest,
    SubscribeToTaskRequest,
    TaskState,
    ListTasksRequest,
    ListTasksResponse,
)
