# tests/mock/a2a_mocks.py
# Shared mock objects extracted from 1_a2a_server_concurrent_task_connection.py + 3_a2a_testing patterns.
# Provides FakeLangChainAgent, mock langfuse fixture, mock handler/app helpers, and consume helpers.
# All in-memory: no Postgres, no LLM API.

import asyncio
import time
import pytest
from unittest.mock import MagicMock

from agent.a2a_connector import ReportA2AExecutor
from agent.report_agent import runtime
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, InMemoryPushNotificationConfigStore
from a2a.server.context import ServerCallContext
from a2a.types.a2a_pb2 import (
    AgentCard,
    AgentCapabilities,
    AgentInterface,
    AgentSkill,
    Message,
    Part,
    Role,
    SendMessageRequest,
    SendMessageConfiguration,
    Task,
    TaskState,
    TaskStatus,
)
from a2a.helpers import get_message_text
from starlette.applications import Starlette
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes


# --- mock langfuse (no real server) ---
mock_langfuse = MagicMock()


@pytest.fixture(autouse=True)
def patch_runtime_langfuse():
    """Replace runtime.langfuse with no-op mock for every test (covers _process_task spans)."""
    original = runtime.langfuse
    runtime.langfuse = mock_langfuse
    yield
    runtime.langfuse = original


@pytest.fixture(autouse=True)
def isolate_global_lock():
    """Ensure global_execution_lock not held across tests (fragile if prior test cancelled before finally release)."""
    from agent.a2a_connector import global_execution_lock

    # start clean
    if global_execution_lock.locked():
        try:
            global_execution_lock.release()
        except RuntimeError:
            pass
    yield
    # end clean — if test left lock held (exception before finally: release), free it
    if global_execution_lock.locked():
        try:
            global_execution_lock.release()
        except RuntimeError:
            pass


# --- FakeLangChainAgent (replaces real LLM) ---
class FakeLangChainAgent:
    """Simulates one full agent run: AIMessage tool_call -> ToolMessage -> final AIMessage.

    delay controls per-step sleep to emulate long runs (for lock timeout tests).
    execution_count tracks astream invocations.
    """

    def __init__(self, delay: float = 0.1):
        self.delay = delay
        self.execution_count = 0

    async def astream(self, *args, **kwargs):
        self.execution_count += 1
        from langchain_core.messages import AIMessage, ToolMessage

        await asyncio.sleep(self.delay)
        yield {"agent": {"messages": [AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": "/test.txt"}, "id": "1"}])]}}
        await asyncio.sleep(self.delay)
        yield {"tools": {"messages": [ToolMessage(content="success", tool_call_id="1")]}}
        await asyncio.sleep(self.delay)
        yield {"agent": {"messages": [AIMessage(content="Report generated successfully.")]}}


class FakeStreamingAgent:
    """Agent that yields many status-like steps to test streaming completeness.

    Emits `steps` intermediate status updates with custom text before final artifact.
    Used to inject 4-5 updates + COMPLETED for streaming tests.
    """

    def __init__(self, steps: int = 5, delay: float = 0.05, final_text: str = "Final report"):
        self.steps = steps
        self.delay = delay
        self.final_text = final_text
        self.execution_count = 0

    async def astream(self, *args, **kwargs):
        self.execution_count += 1
        from langchain_core.messages import AIMessage, ToolMessage

        for i in range(self.steps):
            await asyncio.sleep(self.delay)
            # Simulate tool call progress as AIMessage tool_calls
            yield {"agent": {"messages": [AIMessage(content="", tool_calls=[{"name": f"step_{i}", "args": {"i": i}, "id": str(i)}])]}}
            await asyncio.sleep(self.delay)
            yield {"tools": {"messages": [ToolMessage(content=f"step {i} ok", tool_call_id=str(i))]}}
        await asyncio.sleep(self.delay)
        yield {"agent": {"messages": [AIMessage(content=self.final_text)]}}


# --- handler / app helpers ---
def create_mock_request_handler(executor: ReportA2AExecutor) -> DefaultRequestHandler:
    """In-memory handler: InMemoryTaskStore + InMemoryPushNotificationConfigStore, streaming True."""
    agent_card = AgentCard(
        name="Test Agent",
        description="Test Agent for Mocking",
        capabilities=AgentCapabilities(streaming=True, push_notifications=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        supported_interfaces=[AgentInterface(protocol_binding="JSONRPC", url="http://localhost")],
        skills=[AgentSkill(id="test", name="Test", description="Test")],
    )
    return DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
        push_config_store=InMemoryPushNotificationConfigStore(),
    )


def create_mock_send_request(context_id: str, text: str = "Generate report") -> SendMessageRequest:
    msg = Message(message_id="test-msg", role=Role.ROLE_USER, parts=[Part(text=text)], context_id=context_id)
    return SendMessageRequest(message=msg, configuration=SendMessageConfiguration(return_immediately=False))


def create_mock_starlette_app(handler: DefaultRequestHandler) -> Starlette:
    """Starlette app wrapping handler JSON-RPC + card routes (for httpx ASGI transport)."""
    routes = []
    routes.extend(create_agent_card_routes(handler._agent_card))  # type: ignore[attr-defined]
    # fallback: if _agent_card private, use handler._agent_card or handler.agent_card
    # SDK stores as _agent_card; alternative is handler._card
    if not routes:
        # try public accessor if private missing (defensive)
        card = getattr(handler, "_agent_card", None) or getattr(handler, "agent_card", None) or getattr(handler, "_card", None)
        if card:
            routes.extend(create_agent_card_routes(card))
    routes.extend(create_jsonrpc_routes(handler, "/"))
    return Starlette(routes=routes)


# --- consume helper ---
async def consume_stream(handler, req, context):
    """Consume handler.on_message_send_stream to list[(timestamp, event)] (for server tests)."""
    try:
        results = []
        async for item in handler.on_message_send_stream(req, context):
            results.append((time.time(), item))
        return results
    except Exception as e:
        return e


# --- helpers to inject tasks directly into store (for get/list discovery) ---
async def inject_task(
    handler: DefaultRequestHandler,
    task_id: str,
    context_id: str,
    state: int,
    text: str = "injected",
    history: bool = True,
) -> Task:
    """Create Task proto and save into handler's InMemoryTaskStore; returns saved task."""
    msg = Message(message_id=f"inject-{task_id}", role=Role.ROLE_AGENT, parts=[Part(text=text)], context_id=context_id)
    task = Task(
        id=task_id,
        context_id=context_id,
        status=TaskStatus(state=state, message=msg),
        history=[msg] if history else [],
    )
    await handler.task_store.save(task, ServerCallContext())
    return task
