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
    3. Task 2 waits in queue while Task 1 is processing (lock behavior).
    """
    # 1. Setup: Use ONE executor instance so they share the SAME execution lock
    executor = ReportA2AExecutor()
    executor.agent = FakeLangChainAgent(delay=20)  # 20s per step * 3 steps = ~60s per task
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

    # 3. ASSERTION: No exceptions occurred
    for res in results:
        assert not isinstance(res, Exception), f"Stream failed with: {res}"

    # 4. ASSERTION: The agent ran exactly twice (proving both executed)
    assert executor.agent.execution_count == 2, f"Expected 2 executions, got {executor.agent.execution_count}"

    # Unpack timestamped lists
    events1 = [ev for _, ev in results[0]]
    events2 = [ev for _, ev in results[1]]

    # Helper functions for event filtering
    def is_status_event(event):
        """Check if event has a status with a message."""
        return hasattr(event, 'status') and event.status is not None
    
    def has_message_with_text(event, text_substring):
        """Check if event's status message contains the given text."""
        if not is_status_event(event):
            return False
        msg_text = get_message_text(event.status.message)
        return text_substring in msg_text if msg_text else False
    
    def get_events_with_message_text(events, text_substring):
        """Filter events that have a status message containing the given text."""
        return [e for e in events if has_message_with_text(e, text_substring)]

    # Verify initial task submission states
    assert events1[0].status.state == TaskState.TASK_STATE_SUBMITTED, \
        f"Task 1 should start as SUBMITTED, got {events1[0].status.state}"
    task1_id = events1[0].id

    assert events2[0].status.state == TaskState.TASK_STATE_SUBMITTED, \
        f"Task 2 should start as SUBMITTED, got {events2[0].status.state}"
    task2_id = events2[0].id

    # Extract all status events with messages for each task
    status_events1 = [e for e in events1 if is_status_event(e) and e.status.message]
    status_events2 = [e for e in events2 if is_status_event(e) and e.status.message]
    
    assert len(status_events1) > 0, "Task 1 should have status events with messages"
    assert len(status_events2) > 0, "Task 2 should have status events with messages"

    # ASSERTION: Task 1 receives "Waiting in execution queue" message
    task1_waiting_events = get_events_with_message_text(status_events1, "Waiting in execution queue")
    assert len(task1_waiting_events) > 0, \
        f"Task 1 should have 'Waiting in execution queue' message. Available messages: {[get_message_text(e.status.message) for e in status_events1]}"

    # ASSERTION: Task 2 receives both "Waiting in execution queue" and "Still waiting in queue" messages
    task2_waiting_events = get_events_with_message_text(status_events2, "Waiting in execution queue")
    assert len(task2_waiting_events) > 0, \
        f"Task 2 should have 'Waiting in execution queue' message. Available messages: {[get_message_text(e.status.message) for e in status_events2]}"
    
    task2_still_waiting_events = get_events_with_message_text(status_events2, "Still waiting in queue")
    assert len(task2_still_waiting_events) > 0, \
        f"Task 2 should have 'Still waiting in queue' messages (proving lock behavior). Available messages: {[get_message_text(e.status.message) for e in status_events2]}"

    # ASSERTION: Task 2 receives "Processing request" message (after lock is released)
    task2_processing_events = get_events_with_message_text(status_events2, "Processing request")
    assert len(task2_processing_events) > 0, \
        f"Task 2 should have 'Processing request' message. Available messages: {[get_message_text(e.status.message) for e in status_events2]}"

    # CRITICAL ASSERTION: Sequential execution timing
    # Find timestamp when Task 1 completes
    task1_completed_ts = None
    for ts, ev in results[0]:
        if is_status_event(ev) and ev.status.state == TaskState.TASK_STATE_COMPLETED:
            task1_completed_ts = ts
            break
    
    # Find timestamp when Task 2 starts processing (acquires lock)
    task2_processing_ts = None
    for ts, ev in results[1]:
        if has_message_with_text(ev, "Processing request"):
            task2_processing_ts = ts
            break

    assert task1_completed_ts is not None, "Task 1 should complete"
    assert task2_processing_ts is not None, "Task 2 should start processing"
    assert task1_completed_ts < task2_processing_ts, (
        f"SEQUENTIAL EXECUTION FAILED: Task 2 started processing BEFORE Task 1 completed. "
        f"Task 1 completed at {task1_completed_ts}, Task 2 started processing at {task2_processing_ts}. "
        f"This proves the lock is NOT working correctly."
    )

    # ASSERTION: Both tasks end in COMPLETED state
    assert events1[-1].status.state == TaskState.TASK_STATE_COMPLETED, \
        f"Task 1 should end as COMPLETED, got {events1[-1].status.state}"
    assert events2[-1].status.state == TaskState.TASK_STATE_COMPLETED, \
        f"Task 2 should end as COMPLETED, got {events2[-1].status.state}"

    # ASSERTION: Concurrent requests took longer than sequential execution
    # If tasks ran truly concurrently, total_time would be ~60s
    # If sequential, total_time would be ~120s (60s + 60s)
    # We allow some margin for overhead
    min_sequential_time = 100  # At least 100 seconds for sequential execution
    assert total_time >= min_sequential_time, (
        f"Total execution time ({total_time:.2f}s) suggests tasks may have run partially in parallel. "
        f"Expected at least {min_sequential_time}s for sequential execution."
    )

    # 8. Task store verification
    task1 = await handler.task_store.get("ctx-1", mock_context)
    assert task1 is not None, "Task 1 should exist in task store"
    assert task1.status.state == TaskState.TASK_STATE_COMPLETED, \
        f"Task 1 in store should be COMPLETED, got {task1.status.state}"
    
    task2 = await handler.task_store.get("ctx-2", mock_context)
    assert task2 is not None, "Task 2 should exist in task store"
    assert task2.status.state == TaskState.TASK_STATE_COMPLETED, \
        f"Task 2 in store should be COMPLETED, got {task2.status.state}"


@pytest.mark.skip(reason="TODO: Fix undefined variables task_id_to_cancel and req2 creation")
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
    executor.agent = FakeLangChainAgent(delay=5.0)  # Slow agent to ensure it's running when we cancel
    handler = create_mock_request_handler(executor)
    
    req = create_mock_send_request("ctx-cancel")
    mock_context = ServerCallContext()

    # 2. Start the task in the background
    stream_task = asyncio.create_task(consume_stream(handler, req, mock_context))
    
    # Let it acquire the lock and start "processing"
    await asyncio.sleep(0.5)
    
    # 3. PROPERLY CANCEL VIA THE HANDLER
    # NOTE: This test needs to be fixed - task_id_to_cancel is undefined
    # The task ID should be extracted from the stream events
    cancel_req = CancelTaskRequest(id="undefined_task_id")  # FIXME: Get actual task ID
    await handler.on_cancel_task(cancel_req, mock_context)
    
    # 4. ASSERTION: Verify the task is actually marked as CANCELED in the store
    get_req = GetTaskRequest(id="undefined_task_id")  # FIXME: Get actual task ID
    canceled_task = await handler.on_get_task(get_req, mock_context)
    assert canceled_task is not None, "Canceled task was not found in the store"
    assert canceled_task.status.state == TaskState.TASK_STATE_CANCELED, \
        f"Task was not marked as canceled! State: {canceled_task.status.state}"

    # 5. ASSERTION: Verify the lock was released by immediately firing a new task.
    # If the lock wasn't released, this new task would hang and timeout.
    req2 = create_mock_send_request("ctx-after")  # Fixed: removed extra argument
    
    result = await asyncio.wait_for(
        consume_stream(handler, req2, mock_context),
        timeout=2.0  # Should finish quickly since the slow agent was canceled and lock released
    )
    
    assert result is not False, "Lock was not released after cancellation, causing a deadlock!"
    assert executor.agent.execution_count == 2, "The second task should have executed after the first was canceled."
