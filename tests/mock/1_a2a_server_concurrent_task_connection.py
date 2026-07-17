# tests/mock/1_a2a_server_concurrent_task_connection.py

import asyncio
import time
import pytest

# Import the REAL components
from agent.a2a_connector import ReportA2AExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.tasks import InMemoryTaskStore, InMemoryPushNotificationConfigStore
from a2a.server.context import ServerCallContext
from a2a.types.a2a_pb2 import (
    AgentCard, AgentCapabilities, AgentInterface, AgentSkill,
    Message, Part, Role, SendMessageRequest, SendMessageConfiguration,
    CancelTaskRequest, GetTaskRequest, TaskState
)
from a2a.helpers import get_message_text

# ==========================================
# 1. THE MOCK DEPENDENCIES
# ==========================================

class FakeLangChainAgent:
    """Simulates the LangChain agent, yielding proper LangChain message objects."""
    def __init__(self, delay: float = 0.1):
        self.delay = delay
        self.execution_count = 0

    async def astream(self, *args, **kwargs):
        self.execution_count += 1
        from langchain_core.messages import AIMessage, ToolMessage
        
        await asyncio.sleep(self.delay)
        yield {"agent": {"messages": [
            AIMessage(content="", tool_calls=[{"name": "read_file", "args": {"file_path": "/test.txt"}, "id": "1"}])
        ]}}
        
        await asyncio.sleep(self.delay)
        yield {"tools": {"messages": [
            ToolMessage(content="success", tool_call_id="1")
        ]}}
        
        await asyncio.sleep(self.delay)
        yield {"agent": {"messages": [
            AIMessage(content="Report generated successfully.")
        ]}}


def create_mock_request_handler(executor: ReportA2AExecutor) -> DefaultRequestHandler:
    """Sets up the full A2A SDK stack in memory."""
    
    agent_card = AgentCard(
        name="Test Agent",
        description="Test Agent for Mocking",
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        supported_interfaces=[
            AgentInterface(protocol_binding="JSONRPC", url="http://localhost")
        ],
        skills=[AgentSkill(id="test", name="Test", description="Test")],
    )
    
    return DefaultRequestHandler(
        agent_executor=executor,
        task_store=InMemoryTaskStore(),
        agent_card=agent_card,
        push_config_store=InMemoryPushNotificationConfigStore(),
    )


def create_mock_send_request(context_id: str) -> SendMessageRequest:
    msg = Message(
        message_id="test-msg",
        role=Role.ROLE_USER,
        parts=[Part(text="Generate report")],
        context_id=context_id
    )
    return SendMessageRequest(
        message=msg,
        configuration=SendMessageConfiguration(return_immediately=False)
    )


# ==========================================
# 2. HELPER TO CONSUME STREAMS
# ==========================================
async def consume_stream(handler, req, context):
    """Wraps an async generator stream into a standard coroutine that asyncio.gather can handle."""
    try:
        results = []
        async for item in handler.on_message_send_stream(req, context):
            results.append((time.time(), item))
        return results
    except Exception as e:
        return e


# ==========================================
# 3. THE INTEGRATION TESTS
# ==========================================

@pytest.mark.asyncio
async def test_sequential_execution_and_state_updates():
    """
    Proves that:
    1. Two concurrent requests are forced to run sequentially (timing check).
    2. Both tasks are successfully created and reach TASK_STATE_COMPLETED in the store.
    """
    # 1. Setup: Use ONE executor instance so they share the SAME execution lock
    executor = ReportA2AExecutor()
    executor.agent = FakeLangChainAgent(delay=20) # 20s per step * 3 steps = ~60s per task
    handler = create_mock_request_handler(executor)
    
    req1 = create_mock_send_request("ctx-1")
    req2 = create_mock_send_request("ctx-2")
    mock_context = ServerCallContext()

    # 2. Fire both requests concurrently
    start_time = time.time()
    results = await asyncio.gather(
        consume_stream(handler, req1, mock_context),
        consume_stream(handler, req2, mock_context),
        return_exceptions=True
    )
    total_time = time.time() - start_time

    # req 1 get task submitted TAKES generated task.id
    # req 2 get task submitted, TAKES generated task.id (a2a server do not work with self-created task-ids, task-id generation is enforced by server instead)
    # req 1 get task working
    # req 2 gets status update occupied check
    # req 1 get task artifacts, completed
    # req 2 get status working
    # req 2 get status artifacts, completed
    # 3. ASSERTION: No exceptions occurred
    for res in results:
        assert not isinstance(res, Exception), f"Stream failed with: {res}"


    # debugging    
    for i, events in enumerate(results, 1):
        print(f"--- Stream {i} ---")
        for ev in events:
            print(ev)


    # 4. ASSERTION: The agent ran exactly twice (proving both executed)
    assert executor.agent.execution_count == 2, f"Expected 2 executions, got {executor.agent.execution_count}"

    # Unpack timestamped lists
    events1 = [ev for _, ev in results[0]]
    events2 = [ev for _, ev in results[1]]

    # Each event is a Task object (or maybe a status update).
    # The first event in each stream is the “task submitted” Task with an auto‑generated id.
    assert events1[0].status.state == TaskState.TASK_STATE_SUBMITTED
    task1_id = events1[0].id

    assert events2[0].status.state == TaskState.TASK_STATE_SUBMITTED
    task2_id = events2[0].id

    # Now the actual sequence: Task 1 should go to WORKING, then COMPLETED,
    # and *only then* should Task 2 go to WORKING.
    # We can extract the status events:
    def is_status_event(event):
        return hasattr(event, 'status') and event.status.message

    status1 = [e for e in events1 if is_status_event(e)]
    status2 = [e for e in events2 if is_status_event(e)]


    # Task 1: first status after submitted should be "Task received. Waiting..."
    assert "Waiting in execution queue" in get_message_text(status1[0].status.message)

    # Task 2: first status should also be "Task received. Waiting..."
    assert "Waiting in execution queue" in get_message_text(status2[0].status.message)

    # Task 1 should complete before Task 2 starts processing.
    # The "Processing request..." message for Task 2 must have a timestamp
    # later than Task 1's COMPLETED status.
    # We can compare the timestamps we captured.

    # Task 2 waiting in queue, due to lock
    assert "Still waiting in queue" in get_message_text(status2[0].status.message)

    # Find indices of key events
    task1_completed_ts = None
    task2_processing_ts = None

    for ts, ev in results[0]:
        if is_status_event(ev) and ev.status.state == TaskState.TASK_STATE_COMPLETED:
            task1_completed_ts = ts
            break

    for ts, ev in results[1]:
        if is_status_event(ev) and "Processing request" in get_message_text(ev.status.message):
            task2_processing_ts = ts
            break

    assert task1_completed_ts is not None, "Task 1 did not complete"
    assert task2_processing_ts is not None, "Task 2 did not start processing"
    assert task1_completed_ts < task2_processing_ts, (
        f"Task 2 started processing before Task 1 completed: "
        f"task1_completed at {task1_completed_ts}, task2_processing at {task2_processing_ts}"
    )

    # Both tasks should end in COMPLETED state
    assert events1[-1].status.state == TaskState.TASK_STATE_COMPLETED
    assert events2[-1].status.state == TaskState.TASK_STATE_COMPLETED

    # The crucial check: Task 1 must be COMPLETED before Task 2 actually starts.
    # A simple way: the timestamp of the last WORKING event of Task 1 should be
    # earlier than the first real "processing" event of Task 2.
    # For a more precise check, look at the message content.
    # Example: the first status of Task 2 contains "Task received. Waiting in execution queue...",
    # and the status that says "Processing request..." only appears after the lock is acquired.
    # We can assert that Task 1's COMPLETED event appears before Task 2's "Processing request".

    # 8. Task store verification (optional, but good)
    task1 = await handler.task_store.get("ctx-1", mock_context)
    assert task1.status.state == TaskState.TASK_STATE_COMPLETED
    task2 = await handler.task_store.get("ctx-2", mock_context)
    assert task2.status.state == TaskState.TASK_STATE_COMPLETED

@pytest.mark.skip()
@pytest.mark.asyncio
async def test_cancellation_via_handler_and_lock_release():
    """
    Proves that:
    1. Calling handler.on_cancel_task properly triggers the SDK's cancellation flow.
    2. The task state in the TaskStore is updated to TASK_STATE_CANCELED.
    3. The execution lock is released, allowing a subsequent task to run immediately.
    """
    # 1. Setup: Use ONE executor instance
    executor = ReportA2AExecutor()
    executor.agent = FakeLangChainAgent(delay=5.0) # Slow agent to ensure it's running when we cancel
    handler = create_mock_request_handler(executor)
    
    req = create_mock_send_request( "ctx-cancel")
    mock_context = ServerCallContext()

    # 2. Start the task in the background
    stream_task = asyncio.create_task(consume_stream(handler, req, mock_context))
    
    # Let it acquire the lock and start "processing"
    await asyncio.sleep(0.5)
    
    # 3. PROPERLY CANCEL VIA THE HANDLER
    cancel_req = CancelTaskRequest(id=task_id_to_cancel)
    await handler.on_cancel_task(cancel_req, mock_context)
    
    # 4. ASSERTION: Verify the task is actually marked as CANCELED in the store
    get_req = GetTaskRequest(id=task_id_to_cancel)
    canceled_task = await handler.on_get_task(get_req, mock_context)
    assert canceled_task is not None, "Canceled task was not found in the store"
    assert canceled_task.status.state == TaskState.TASK_STATE_CANCELED, \
        f"Task was not marked as canceled! State: {canceled_task.status.state}"

    # 5. ASSERTION: Verify the lock was released by immediately firing a new task.
    # If the lock wasn't released, this new task would hang and timeout.
    req2 = create_mock_send_request("task-after-cancel", "ctx-after")
    
    result = await asyncio.wait_for(
        consume_stream(handler, req2, mock_context),
        timeout=2.0 # Should finish quickly since the slow agent was canceled and lock released
    )
    
    assert result is True, "Lock was not released after cancellation, causing a deadlock!"
    assert executor.agent.execution_count == 2, "The second task should have executed after the first was canceled."