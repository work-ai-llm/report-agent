# tests/mock/1_a2a_server_concurrent_task_connection.py
#
# ============================================================================
# PURPOSE OF THIS FILE (uses shared mocks from tests/mock/a2a_mocks.py)
# ============================================================================
# These tests validate TWO SEPARATE layers of the A2A report server WITHOUT any
# external services. All real dependencies are swapped for in-memory stand-ins
# now imported from a2a_mocks.py:
#
#   FakeLangChainAgent  → replaces the real LangChain agent (no LLM API calls,
#                         no token cost). Simulates a full agent run by yielding
#                         a tool call, a tool result, and a final answer.
#   MagicMock           → replaces runtime.langfuse (no Langfuse server reachable).
#                         _process_task() uses langfuse spans as context managers;
#                         a MagicMock satisfies that protocol with zero setup.
#   InMemoryTaskStore   → replaces the Postgres-backed task store.
#
# The two layers under test:
#
#   1. test_sequential_execution_and_state_updates
#      → CONCURRENCY LOCKING. Proves the global execution lock (in
#        ReportA2AExecutor) serializes concurrent client requests so exactly one
#        agent runs at a time, while every request still completes.
#
#   2. test_cancellation_via_handler_and_lock_release
#      → TASK LIFECYCLE. Proves a CancelTaskRequest stops a running task, marks
#        it TASK_STATE_CANCELED in the store, and frees the execution lock so the
#        next task runs immediately.
#
# NOTE: Layer 1 and layer 2 test DIFFERENT things. The lock does NOT manage the
# task lifecycle; the task lifecycle is managed by the A2A SDK. They interact:
# cancelling a running task releases the lock it held, which is why the lifecycle
# test also asserts the lock is free afterwards.
# See a2a_mocks.py for shared definitions; this file keeps the original
# purpose/logic docs and assertions intact, just imports mocks centrally.

import asyncio
import time
import pytest

from agent.a2a_connector import ReportA2AExecutor
from a2a.server.context import ServerCallContext
from a2a.types.a2a_pb2 import CancelTaskRequest, GetTaskRequest, TaskState
from a2a.helpers import get_message_text

from tests.mock.a2a_mocks import (
    FakeLangChainAgent,
    create_mock_request_handler,
    create_mock_send_request,
    consume_stream,
)


# ==========================================
# 4. THE INTEGRATION TESTS
# ==========================================

@pytest.mark.asyncio
async def test_sequential_execution_and_state_updates():
    """
    ============================================================================
    WHAT THIS TEST PROVES
    ============================================================================
    Two client requests fired concurrently are forced to run SEQUENTIALLY by the
    global execution lock, and BOTH still reach TASK_STATE_COMPLETED.

    ----------------------------------------------------------------------------
    THE MECHANISM UNDER TEST (where the lock lives)
    ----------------------------------------------------------------------------
    ReportA2AExecutor.execute() wraps agent processing in a single global
    asyncio.Lock. Before a task may run the agent, it must acquire the lock via
    `asyncio.wait_for(lock.acquire(), timeout=30.0)`. If another task already
    holds the lock, the waiting task loops: every 30s it times out and sends a
    "Still waiting in queue..." status update to its client, until the lock frees.

    WHY THIS MATTERS: without the lock, N concurrent clients would trigger N
    simultaneous LLM calls, overwhelming the Mistral API rate limiter. The lock
    serializes execution to exactly 1 agent run at a time.

    ----------------------------------------------------------------------------
    HOW THE TEST IS STRUCTURED (and why each piece exists)
    ----------------------------------------------------------------------------
    - ONE executor instance for both requests. The lock is module-global, so two
      SEPARATE executors would still serialize — but a single instance mirrors
      the real server and keeps the test honest.
    - FakeLangChainAgent(delay=12). One full run = 3 steps x 12s = 36s. This is
      deliberately LONGER than the 30s lock-wait timeout, which FORCES task 2 to
      hit the timeout at least once and emit "Still waiting in queue...". If the
      delay were shorter than 10s (3xdelay < 30s), task 2 would acquire the lock
      before ever timing out, and the lock behavior would be invisible.
    - Both requests fired via asyncio.gather → they genuinely start together.

    EXPECTED EVENT TIMELINE
      Task 1: SUBMITTED → "Task received. Waiting in execution queue..." →
              acquires lock instantly → "Processing request..." → tool events →
              COMPLETED
      Task 2: SUBMITTED → "Task received. Waiting in execution queue..." →
              BLOCKS on the lock → after 30s sends "Still waiting in queue..." →
              Task 1 finishes at ~36s and releases the lock → Task 2 acquires →
              "Processing request..." → COMPLETED

    ----------------------------------------------------------------------------
    ASSERTIONS → WHAT EACH ONE PROVES
    ----------------------------------------------------------------------------
      1. No exceptions in either stream           → both requests ran cleanly end-to-end.
      2. agent.execution_count == 2               → astream() ran exactly twice; both tasks really executed.
      3. First event of each stream is SUBMITTED  → the SDK created both tasks.
      4. Task 1 got a waiting ACK message         → it entered execute() (the queue path).
      5. Task 2 got >= 1 "Still waiting in queue" → IT WAS BLOCKED BY THE LOCK.
                                                   THIS is the direct proof the lock serializes execution.
      6. Task 2's first "Still waiting" precedes  → it waited FIRST, processed SECOND
         its "Processing request"                   (correct queue discipline).
      7. Task 1 COMPLETED before/at Task 2 start  → task 2's unblocking was caused by task 1 finishing,
         (within 1s margin)                         not by any other reason.
      8. Both streams end COMPLETED               → no task was dropped or left hanging.
      9. Total wall time >= 60s                   → sequential (~72s), not parallel (~36s). Independent smoke check
                                                    that reinforces #5.
     10. Both tasks exist in the TaskStore and    → state persistence is correct after sequential execution.
         are COMPLETED
    ============================================================================
    """
    # 1. Setup: Use ONE executor instance so they share the SAME execution lock
    executor = ReportA2AExecutor()
    executor.agent = FakeLangChainAgent(delay=12)  # 12s per step * 3 steps = ~36s per task (>30s lock timeout)
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

    # ASSERTION: Task 1 receives "Waiting in execution queue" or "Task received. Waiting" message
    # Based on actual output, Task 1 gets "Task received. Waiting in execution queue..." as first WORKING message
    task1_waiting_events = get_events_with_message_text(status_events1, "Waiting in execution queue")
    task1_received_events = get_events_with_message_text(status_events1, "Task received")
    assert len(task1_waiting_events) > 0 or len(task1_received_events) > 0, \
        f"Task 1 should have waiting/received message. Available messages: {[get_message_text(e.status.message) for e in status_events1]}"

    # ASSERTION: Task 2 receives "Still waiting in queue" messages (proving lock behavior)
    # This is the key assertion - Task 2 should show multiple "Still waiting in queue" while Task 1 processes
    task2_still_waiting_events = get_events_with_message_text(status_events2, "Still waiting in queue")
    assert len(task2_still_waiting_events) > 0, \
        f"Task 2 should have 'Still waiting in queue' messages (proving lock behavior). Available messages: {[get_message_text(e.status.message) for e in status_events2]}"

    # ASSERTION: Task 2 receives "Processing request" message (after lock is released)
    task2_processing_events = get_events_with_message_text(status_events2, "Processing request")
    assert len(task2_processing_events) > 0, \
        f"Task 2 should have 'Processing request' message. Available messages: {[get_message_text(e.status.message) for e in status_events2]}"

    # CRITICAL ASSERTION: Sequential execution timing
    # The key proof of lock behavior is that Task 2 receives "Still waiting in queue" messages
    # BEFORE it receives "Processing request", proving it waited for Task 1

    # Find the FIRST "Still waiting in queue" message for Task 2
    task2_first_wait_ts = None
    for ts, ev in results[1]:
        if has_message_with_text(ev, "Still waiting in queue"):
            task2_first_wait_ts = ts
            break

    # Find the FIRST "Processing request" message for Task 2
    task2_processing_ts = None
    for ts, ev in results[1]:
        if has_message_with_text(ev, "Processing request"):
            task2_processing_ts = ts
            break

    # Find timestamp when Task 1 completes
    task1_completed_ts = None
    for ts, ev in results[0]:
        if is_status_event(ev) and ev.status.state == TaskState.TASK_STATE_COMPLETED:
            task1_completed_ts = ts
            break

    assert task1_completed_ts is not None, "Task 1 should complete"
    assert task2_processing_ts is not None, "Task 2 should start processing"

    # The critical assertion: Task 2 must have received "Still waiting in queue" messages
    # This proves it was blocked waiting for Task 1
    assert task2_first_wait_ts is not None, \
        "Task 2 should have 'Still waiting in queue' messages (proving lock behavior)"

    # Verify the order: Task 2 waits BEFORE it processes
    assert task2_first_wait_ts < task2_processing_ts, (
        f"LOCK BEHAVIOR FAILED: Task 2 started processing BEFORE it showed waiting messages. "
        f"First wait at {task2_first_wait_ts}, processing at {task2_processing_ts}."
    )

    # Additional verification: Task 1 should complete around the same time Task 2 starts processing
    # (allowing small margin for timing precision)
    time_diff = task2_processing_ts - task1_completed_ts
    assert time_diff > -1.0, (
        f"SEQUENTIAL EXECUTION QUESTIONABLE: Task 2 started processing more than 1s before Task 1 completed. "
        f"Task 1 completed at {task1_completed_ts}, Task 2 started processing at {task2_processing_ts}. "
        f"Difference: {time_diff:.3f}s"
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
    min_sequential_time = 60  # At least 60 seconds for sequential execution (12s*3steps * 2tasks = 72s)
    assert total_time >= min_sequential_time, (
        f"Total execution time ({total_time:.2f}s) suggests tasks may have run partially in parallel. "
        f"Expected at least {min_sequential_time}s for sequential execution."
    )

    # 8. Task store verification - use actual task IDs from the events
    task1 = await handler.task_store.get(task1_id, mock_context)
    assert task1 is not None, f"Task 1 (id={task1_id}) should exist in task store"
    assert task1.status.state == TaskState.TASK_STATE_COMPLETED, \
        f"Task 1 in store should be COMPLETED, got {task1.status.state}"

    task2 = await handler.task_store.get(task2_id, mock_context)
    assert task2 is not None, f"Task 2 (id={task2_id}) should exist in task store"
    assert task2.status.state == TaskState.TASK_STATE_COMPLETED, \
        f"Task 2 in store should be COMPLETED, got {task2.status.state}"


@pytest.mark.asyncio
async def test_cancellation_via_handler_and_lock_release():
    """
    ============================================================================
    WHAT THIS TEST PROVES
    ============================================================================
    A CancelTaskRequest sent through the SDK's request handler:
      1. stops the running task via the SDK's internal cancellation flow,
      2. leaves the task marked TASK_STATE_CANCELED in the TaskStore, and
      3. releases the global execution lock so the NEXT task runs immediately.

    ----------------------------------------------------------------------------
    CONTEXT 1 — WHY CANCELLATION FREES THE LOCK (this comes for free)
    ----------------------------------------------------------------------------
    execute() holds the lock inside a try/finally:
        try:    await self._process_task(...)
        finally: global_execution_lock.release()
    When the SDK cancels a task it raises asyncio.CancelledError inside
    execute(). That exception unwinds the try, so the finally ALWAYS releases the
    lock — whether a run completes normally or is cancelled midway.

    ----------------------------------------------------------------------------
    CONTEXT 2 — WHAT THE EXECUTOR'S cancel() METHOD DOES AND DOES NOT DO
    ----------------------------------------------------------------------------
    Stopping the task is done ENTIRELY by the SDK internals
    (ActiveTask.cancel() → producer_task.cancel() raises CancelledError in
    execute()). The executor's own cancel() does NOT stop anything. Its only job
    is to publish a TaskStatusUpdateEvent(TASK_STATE_CANCELED) to the event
    queue. That event flows through the SDK consumer into the TaskStore so that
    clients querying task state see CANCELED instead of a stale WORKING.

    So: the SDK stops the work; our cancel() informs the client.

    ----------------------------------------------------------------------------
    WHY WE EXTRACT THE TASK ID (instead of reusing our context id)
    ----------------------------------------------------------------------------
    CancelTaskRequest targets a SPECIFIC task, not a context. Our request sets
    context_id ("ctx-cancel"), but each task gets its OWN auto-generated task_id
    (UUID) from the SDK. So we cannot cancel "the context"; we must cancel the
    concrete task. We grab its id from the first (SUBMITTED) event of the stream
    while it runs in the background.

    ----------------------------------------------------------------------------
    STEP-BY-STEP NARRATIVE
    ----------------------------------------------------------------------------
      1. Send one request in the background with a SLOW agent (5s/step) so it is
         still mid-run when we cancel. Slow matters: if it finished instantly,
         there would be nothing left to cancel.
      2. Capture the task id from the first event of the background stream.
      3. Wait 0.5s → lets the task acquire the lock and actually start processing.
      4. Call handler.on_cancel_task(CancelTaskRequest(id=...)). This is the
         real SDK path a client's cancel request goes through.
      5. Call handler.on_get_task() and assert the task is CANCELED → proves the
         SDK stopped it and our cancel() event updated the store.
      6. Fire a SECOND request with a 20s timeout and assert it completes →
         proves the lock was released. If the lock were still held, this second
         task would loop sending "Still waiting in queue..." forever and the 20s
         timeout would expire (test failure).
      7. agent.execution_count == 2 → run #1 was the cancelled task's astream
         beginning (count=1) and run #2 was the post-cancel task completing
         (count=2). Confirms both tasks actually reached the agent.

    ----------------------------------------------------------------------------
    WHY THE 20s TIMEOUT IS NOT "just a generous limit"
    ----------------------------------------------------------------------------
    The post-cancel task runs ~15s (3 steps x 5s). A 20s timeout only trips if
    the pipeline is STUCK — the specific stuck condition this test hunts for is
    "lock never released, second task queue-blocked forever".
    ============================================================================
    """
    # 1. Setup: Use ONE executor instance
    executor = ReportA2AExecutor()
    executor.agent = FakeLangChainAgent(delay=5.0)  # Slow agent to ensure it's running when we cancel
    handler = create_mock_request_handler(executor)

    req = create_mock_send_request("ctx-cancel")
    mock_context = ServerCallContext()

    # Shared state to capture task ID from stream
    events_buffer = []
    first_event_received = asyncio.Event()

    async def consume_stream_tracked(handler, req, context):
        try:
            async for item in handler.on_message_send_stream(req, context):
                events_buffer.append((time.time(), item))
                if not first_event_received.is_set():
                    first_event_received.set()
            return events_buffer
        except Exception as e:
            return e

    # 2. Start the task in the background
    stream_task = asyncio.create_task(consume_stream_tracked(handler, req, mock_context))

    # Wait for the first event (SUBMITTED) which contains the task ID
    await first_event_received.wait()
    task_id_to_cancel = events_buffer[0][1].id

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
    req2 = create_mock_send_request("ctx-after")

    result = await asyncio.wait_for(
        consume_stream(handler, req2, mock_context),
        timeout=20.0  # Should finish quickly since the slow agent was canceled and lock released
    )

    assert not isinstance(result, Exception), f"Lock was not released after cancellation: {result}"
    assert executor.agent.execution_count == 2, "The second task should have executed after the first was canceled."
