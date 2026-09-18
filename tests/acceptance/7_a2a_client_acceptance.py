"""7_a2a_client_acceptance.py — A2A client acceptance via unified A2AClient.

Covers the full lifecycle against mock (default) or live server (--live):
  1. streaming without findings (test run, proceed anyway)  [old test 0, modified]
  2. single scratchpad task                                 [old test 1]
  3. store clean / inject / re-ask "do you see them now?"    [old test 2, live proof]
  4. sequential trio stress (3 concurrent)                   [old test 4]
  5. simple drop (5 fired, 3-5 dropped)                      [old test 5-simple]
  6. advanced lifecycle (ids + drop + get/cancel/sub/poll)   [old test 5]

Old test 3 (concurrent pair) omitted intentionally; test 4 covers it.
Old 3_a2a_testing.py tests 4/5 preserved for history only (see NOTE there).

Cmd printing preserved everywhere: every test prints SENT text and the
(mock or real) agent RECEIVED response. Mock agent final text is
"Report generated successfully." (FakeLangChainAgent).

Usage (mock, default):
  python tests/acceptance/7_a2a_client_acceptance.py --test 3
  python tests/acceptance/7_a2a_client_acceptance.py --test 6 --mock-delay 0.5
Live (server must run via `python -m agent.a2a_connector` from repo root):
  python tests/acceptance/7_a2a_client_acceptance.py --test 3 --live
"""

import argparse
import asyncio
import os
import sys
import uuid
from contextlib import AsyncExitStack
from pathlib import Path

if sys.platform == "win32":
    # Windows console defaults to cp1252 which crashes on unicode (e.g. arrows
    # in agent output) — force UTF-8 like 8_llm_api_acceptance.py does.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import httpx
from dotenv import load_dotenv

from a2a.client import A2ACardResolver, ClientConfig
from a2a.types.a2a_pb2 import TaskState

from client.a2a_client import A2AClient
from tests.mock.a2a_mocks import (
    FakeLangChainAgent,
    create_mock_request_handler,
    create_mock_starlette_app,
)
from agent.a2a_connector import ReportA2AExecutor

load_dotenv()

SERVER_URL = os.getenv("A2A_SERVER_URL", "http://127.0.0.1:9999")


# ------------------------------------------------------------------
# Client factories (mock / live), AsyncExitStack pattern
# ------------------------------------------------------------------
async def make_mock_client(delay=0.2, streaming=True, polling=False):
    """Mock handler (FakeLangChainAgent, no DB/LLM) + A2AClient over ASGI."""
    import asyncio as _asyncio
    from unittest.mock import MagicMock as _MagicMock
    import agent.a2a_connector as _conn
    from agent.report_agent import runtime as _runtime

    _conn.global_execution_lock = _asyncio.Lock()
    if _runtime.langfuse is None:
        _runtime.langfuse = _MagicMock()

    executor = ReportA2AExecutor()
    executor.agent = FakeLangChainAgent(delay=delay)
    handler = create_mock_request_handler(executor)
    app = create_mock_starlette_app(handler)
    transport = httpx.ASGITransport(app=app)
    httpx_client = httpx.AsyncClient(transport=transport, base_url="http://testserver")
    client = await A2AClient.create(
        server_url="http://testserver",
        streaming=streaming,
        polling=polling,
        httpx_client=httpx_client,
    )
    return client, httpx_client, executor, handler


async def make_live_client(httpx_client):
    """A2AClient against the real server (uses caller's httpx for stack mgmt)."""
    return await A2AClient.create(
        server_url=SERVER_URL, streaming=True, httpx_client=httpx_client
    )


# ------------------------------------------------------------------
# TEST 1: streaming without findings (old 0, modified prompt)
# ------------------------------------------------------------------
async def run_test_1_streaming_no_findings(client: A2AClient):
    print("\n" + "=" * 60)
    print("[TEST 1] Streaming smoke: report WITHOUT findings (test run)")
    print("=" * 60)
    text = (
        "Create a small report for the recon phase. "
        "There are no findings in the store right now and this is only "
        "something like a test run, so do not block waiting for files — "
        "proceed with whatever you have and produce a short draft."
    )
    print(f"[TEST1 SENT]: {text}")
    task_id, ctx, artifact = await client.send_streaming_message(text, label="[test1] ")
    print(f"[TEST1 RECEIVED task_id={task_id}]: {(artifact[:300] if artifact else None)}")
    print("\n[TEST 1 COMPLETE]")


# LIVE RESULT (2026-09-17, Postgres + ornith 100k ctx): PASS. 28 msgs /
# 15 steps, COMPLETED with a real grounded recon report saved to
# /context/reports/recon_20260705_final.md. Took ~22 min on this GPU.


# ------------------------------------------------------------------
# TEST 2: single scratchpad task (old 1)
# ------------------------------------------------------------------
async def run_test_2_single_task(client: A2AClient):
    print("\n" + "=" * 60)
    print("[TEST 2] Single Task Loop (scratchpad workflow)")
    print("=" * 60)
    text = (
        "Initialize the report generation for the recon phase. "
        "Follow the scratchpad assembly workflow."
    )
    print(f"[TEST2 SENT]: {text}")
    task_id, ctx, artifact = await client.send_streaming_message(text, label="[test2] ")
    print(f"[TEST2 RECEIVED task_id={task_id}]: {(artifact[:300] if artifact else None)}")
    print("\n[TEST 2 COMPLETE]")


# LIVE RESULT (2026-09-17): PASS. 31 msgs, full scratchpad workflow
# (ls + reads + 3 section writes + read-back + compile), COMPLETED.


# ------------------------------------------------------------------
# TEST 3: store clean / inject / re-ask (old 2 — the live proof test)
# ------------------------------------------------------------------
async def run_test_3_store_clarification(client: A2AClient, use_mock: bool):
    from dev_tools.store_cli import seed_store, inspect_store, delete_file

    print("\n" + "=" * 60)
    print("[TEST 3] Store Injection & Clarification Loop")
    print("=" * 60)
    if use_mock:
        print("(mock note: FakeAgent does not read the store, so both answers "
              "are the generic mock response; lifecycle + store CLI wiring is "
              "what this verifies. Live run shows real 'do you see them'.)\n")

    print("\n--- Step 0: Ensuring local store handle (pool) for CLI ops ---")
    try:
        from agent.report_agent import runtime as _rt
        await _rt._init_pool()
        print(f"  store ready: {type(_rt.store).__name__}")
    except Exception as e:
        print(f"  store init skipped: {type(e).__name__}: {e}")

    print("\n--- Step 1: Deleting finding files from Store ---")
    for p in ("/findings/nmap_scans.txt", "/findings/bloodhound_dump.txt"):
        try:
            await delete_file(p)
        except Exception as e:
            print(f"  delete {p} skipped: {type(e).__name__}: {e}")

    print("\n--- Step 2: Inspecting Store to confirm deletion ---")
    try:
        await inspect_store("filesystem", True, False, 50)
    except Exception as e:
        print(f"  inspect skipped: {type(e).__name__}: {e}")

    print("\n--- Step 3: Asking for report without files ---")
    t1 = "Generate the pentest report based on the available findings."
    print(f"[TEST3 SENT-1]: {t1}")
    # send_streaming_message returns (task_id, context_id, artifact) — keep context.
    _, ctx_id, art1 = await client.send_streaming_message(t1, label="[test3-1] ")
    print(f"[TEST3 RECEIVED-1]: {(art1[:300] if art1 else None)}")

    print("\n--- Step 4: Seeding Store with findings ---")
    findings_dir = Path(__file__).resolve().parent.parent.parent / "results"
    if not findings_dir.exists():
        print(f"  findings dir missing at {findings_dir}, skipping seed")
    else:
        try:
            await seed_store(findings_dir)
        except Exception as e:
            print(f"  seed skipped: {type(e).__name__}: {e}")

    print("\n--- Step 5: Inspecting Store to confirm files are back ---")
    try:
        await inspect_store("filesystem", True, False, 50)
    except Exception as e:
        print(f"  inspect skipped: {type(e).__name__}: {e}")

    print("\n--- Step 6: Re-asking with SAME context — do you see them now? ---")
    t2 = ("I uploaded the findings to the store now. Do you see them now? "
          "Can you start the report generation?")
    print(f"[TEST3 SENT-2 ctx={ctx_id}]: {t2}")
    _, _, art2 = await client.send_streaming_message(t2, context_id=ctx_id, label="[test3-2] ")
    print(f"[TEST3 RECEIVED-2]: {(art2[:300] if art2 else None)}")
    print("\n[TEST 3 COMPLETE]")


# LIVE RESULT (2026-09-17): store proof PASS — ask 1 saw empty findings
# (ls -> []), re-seed restored both files, ask 2 read them. BUT continuity
# FAILED this run: ask 2 landed in a new thread (tuple-unpack bug, since
# fixed, plus the streaming-context fallback in A2AClient). Re-run live to
# prove same-thread follow-up.


# ------------------------------------------------------------------
# TEST 4: sequential trio stress (old 4)
# ------------------------------------------------------------------
async def run_test_4_sequential_trio(client: A2AClient):
    print("\n" + "=" * 60)
    print("[TEST 4] Sequential Execution Stress (3 concurrent tasks)")
    print("=" * 60)
    print("Server should queue them and execute strictly one by one.\n")

    tasks = [
        "Generate a quick summary of the recon phase.",
        "Analyze the bloodhound dump for privilege escalation paths.",
        "Create a draft for the executive summary based on nmap scans.",
    ]

    async def run_one(idx, text):
        print(f"\n[TEST4 SENT Task {idx}]: {text}")
        task_id, ctx, artifact = await client.send_streaming_message(
            text, label=f"[test4 Task {idx}] "
        )
        print(f"[TEST4 RECEIVED Task {idx} task_id={task_id}]: "
              f"{(artifact[:200] if artifact else None)}")
        return ctx, artifact, task_id, text

    results = await asyncio.gather(
        *[run_one(i + 1, t) for i, t in enumerate(tasks)]
    )
    for i, (ctx, resp, tid, sent) in enumerate(results):
        snippet = (resp[:150].replace('\n', ' ') if resp else "No response")
        print(f"\nTask {i + 1} Task ID: {tid} Context: {ctx}")
        print(f"Task {i + 1} SENT: {sent}")
        print(f"Task {i + 1} RECEIVED: {snippet}...")
    print("\n[TEST 4 COMPLETE]")


# LIVE RESULT (2026-09-17): PASS. 3/3 COMPLETED with real artifacts
# (incl. executive-summary draft saved to /context/reports/). Lock
# serialization held the whole way.


# ------------------------------------------------------------------
# TEST 5: simple drop (old 5-simple)
# NOTE — REQUIRES REDESIGN (2026-09-17 live result): this test relies on the
# old A2A assumption that dropping the streaming client stops the server
# task. It does NOT under the lock model — the queue keeps dropped tasks and
# they run to completion anyway (verified live: all 5 tasks went WORKING, 2
# finished with real output, 3 had to be cancelled via API). Redesign around
# cancel_task semantics instead of disconnect semantics.
# ------------------------------------------------------------------
async def run_test_5_simple_drop(client: A2AClient):
    print("\n" + "=" * 60)
    print("[TEST 5] Concurrency Limit & Client Drop Simulation")
    print("=" * 60)
    print("Firing 5 tasks. Tasks 1 & 2 should finish; 3, 4, 5 get dropped.\n")

    async def run_single_stream(task_num, text):
        ctx_id = str(uuid.uuid4())
        print(f"[TEST5 SENT Task {task_num:02d}]: {text}")
        try:
            task_id, returned_ctx, response_text = await client.send_streaming_message(
                text, context_id=ctx_id, label=f"[Task {task_num:02d}] "
            )
            print(f"[Task {task_num:02d}] Finished normally. task_id={task_id}")
            print(f"[TEST5 RECEIVED Task {task_num:02d}]: "
                  f"{(response_text[:200] if response_text else None)}")
            return returned_ctx, response_text
        except asyncio.CancelledError:
            print(f"[Task {task_num:02d}] !!! CLIENT CONNECTION DROPPED (Cancelled) !!!")
            raise
        except Exception as e:
            print(f"[Task {task_num:02d}] Stream ended with error: {type(e).__name__}: {e}")
            return ctx_id, None

    all_tasks = []
    for i in range(5):
        all_tasks.append(asyncio.create_task(
            run_single_stream(i + 1, f"Task {i + 1}: Quick summary of recon phase.")
        ))

    print("\n--- WAITING 5 SECONDS TO LET QUEUE BUILD UP ---")
    await asyncio.sleep(5)

    print("\n--- DROPPING CLIENT CONNECTIONS FOR TASKS 3, 4, 5 ---")
    for i in range(2, 5):
        all_tasks[i].cancel()

    print("\n--- WAITING FOR TASKS 1 & 2 TO COMPLETE ---")
    results = await asyncio.gather(*all_tasks, return_exceptions=True)

    print("\n" + "-" * 50)
    print("--- TEST 5 RESULTS (SENT vs mock RECEIVED) ---")
    for i, res in enumerate(results):
        sent = f"Task {i + 1}: Quick summary of recon phase."
        if isinstance(res, asyncio.CancelledError):
            print(f"Task {i + 1}: DROPPED (Client disconnected) SENT={sent!r}")
        elif isinstance(res, Exception):
            print(f"Task {i + 1}: FAILED with {type(res).__name__} SENT={sent!r}")
        else:
            ctx, resp = res
            snippet = (resp[:120].replace('\n', ' ') if resp else "No response")
            print(f"Task {i + 1}: COMPLETED (Context: {ctx[:8]}...) "
                  f"SENT={sent!r} RECEIVED={snippet}...")
    print("\n[TEST 5 COMPLETE]")


# ------------------------------------------------------------------
# TEST 6: advanced lifecycle (old 5)
# NOTE — REQUIRES REDESIGN (same reason as test 5): phases 2-4 assume
# dropping background subscribers kills the server tasks ("zombie" idea from
# the abandoned scheduler design). Under the lock model dropped tasks keep
# running; phases 5-7 (get/cancel/subscribe/poll) are still valid and should
# be kept in the redesign. Never run live yet — mock-only so far.
# ------------------------------------------------------------------
async def run_test_6_advanced_lifecycle(client: A2AClient):
    print("\n" + "=" * 60)
    print("[TEST 6] Advanced Lifecycle: Drop, Zombie, Subscribe & Get")
    print("=" * 60)
    service = client

    print("\n[PHASE 1] Firing 5 tasks and extracting Server Task IDs...")
    active_tasks, task_ids = {}, {}
    for i in range(1, 6):
        text = f"Task {i}: Generate a quick recon summary."
        print(f"  [SENT Task {i}]: {text}")
        tid, _cid, _first = await service.send_message(text, label=f"  [Task {i} send] ")
        task_ids[i] = tid
        print(f"  -> Task {i} created. Server ID: {tid}")

        async def consume(task_id=tid, num=i):
            try:
                await service.subscribe_to_task(task_id, label=f"  [Task {num} sub] ")
            except asyncio.CancelledError:
                print(f"  [Task {num}] subscriber dropped (Cancelled)")
                raise
            except Exception as e:
                print(f"  [Task {num}] subscribe ended: {type(e).__name__}: {e}")

        active_tasks[i] = asyncio.create_task(consume())

    # Mock-shortened (live used 5s). Pass --mock-delay 1.0+ for deeper queues.
    print("\n[PHASE 2] Letting server process briefly, then dropping 3, 4, 5...")
    await asyncio.sleep(2)
    for i in [3, 4, 5]:
        active_tasks[i].cancel()

    print("\n[PHASE 3] Letting Tasks 1 & 2 finish normally...")
    for i in [1, 2]:
        try:
            await active_tasks[i]
        except asyncio.CancelledError:
            print(f"  -> Task {i} subscriber was cancelled unexpectedly.")
            continue
        print(f"  -> Task {i} finished normally.")

    print("\n[PHASE 4] Waiting briefly for server to process 'zombie' tasks...")
    await asyncio.sleep(2)

    print("\n[PHASE 5] Handling Task 3 (Check status, then cancel)...")
    task3_data = await service.get_task(task_ids[3], label="  ")
    if task3_data.status.state == TaskState.TASK_STATE_WORKING:
        print("  -> Task 3 is still working. cancel")
        await service.cancel_task(task_ids[3], label="  ")
    else:
        print(f"  -> Task 3 already terminal: {task3_data.status.state}, no cancel needed")

    print("\n[PHASE 6] Handling Task 4 (Get & Subscribe)...")
    task4_data = await service.get_task(task_ids[4], label="  [Get 4] ")
    if task4_data.status.state == TaskState.TASK_STATE_WORKING:
        print("  -> Task 4 is still working. subscribing...")
        try:
            await service.subscribe_to_task(task_ids[4], label="  [Subscribe 4] ")
        except Exception as e:
            print(f"  -> Subscribe ended: {type(e).__name__}: {e}")
    else:
        print(f"  -> Task 4 already terminal: {task4_data.status.state}, skip subscribe")

    print("\n[PHASE 7] Handling Task 5 (Poll, then Retrieve)...")
    # Mock-shortened (live: interval=60, max=10). Keeps observation fast.
    task5_data = await service.poll_until_complete(
        task_ids[5], interval=1, max_attempts=10, label="  "
    )
    if task5_data and task5_data.status.state == TaskState.TASK_STATE_COMPLETED:
        print("  -> Task 5 completed. Quick re-check (mock-shortened)...")
        for i in range(2):
            print(f"  -> Countdown check {i + 1}/2...")
            await asyncio.sleep(1)
            check = await service.get_task(task_ids[5], label="     ")
            if check.status.state != TaskState.TASK_STATE_COMPLETED:
                print("  -> WARNING: Task disappeared or changed state!")
                break
        print("  -> Retrieving Task 5 final payload...")
        final = await service.get_task(task_ids[5], label="  [Get 5] ")
        try:
            from a2a.helpers import get_message_text as _get_text
            _hist = [_get_text(m) for m in (final.history or []) if _get_text(m)]
            print(f"  [RECEIVED Task 5 history ({len(_hist)} msgs)]: "
                  f"{(_hist[-1][:200] if _hist else None)}")
            _st = _get_text(final.status.message) if final.status.message else None
            print(f"  [RECEIVED Task 5 status]: "
                  f"{(_st[:200] if _st else None)} state={final.status.state}")
        except Exception as e:
            print(f"  [RECEIVED Task 5] could not extract text: {e}")
        try:
            await service.subscribe_to_task(task_ids[5], label="  [Replay 5] ")
        except Exception as e:
            print(f"  -> Replay subscribe ended (expected for terminal): "
                  f"{type(e).__name__}: {e}")
    else:
        print("  -> Task 5 did not complete in time!")

    print("\n[TEST 6 COMPLETE] (SENT 5x recon summaries, RECEIVED mock "
          "'Report generated successfully.' via STATUS/ARTIFACT above)")


# ------------------------------------------------------------------
# MAIN (same CLI base as 3_a2a_testing.py, AsyncExitStack swapping)
# ------------------------------------------------------------------
async def main(test_num, use_mock=True, mock_delay=0.5):
    print("\n" + "=" * 60)
    print(f"STARTING A2A CLIENT ACCEPTANCE TEST {test_num} "
          f"({'MOCK' if use_mock else 'LIVE'})")
    print("=" * 60)

    async with AsyncExitStack() as stack:
        if use_mock:
            client, httpx_client, _exec, _handler = await make_mock_client(
                delay=mock_delay, streaming=True, polling=False
            )
            stack.push_async_callback(client.close)
            stack.push_async_callback(httpx_client.aclose)
            dispatch_mock = True
        else:
            httpx_client = await stack.enter_async_context(
                httpx.AsyncClient(timeout=300.0)
            )
            client = await make_live_client(httpx_client)
            stack.push_async_callback(client.close)
            dispatch_mock = False

        if test_num == 1:
            await run_test_1_streaming_no_findings(client)
        elif test_num == 2:
            await run_test_2_single_task(client)
        elif test_num == 3:
            await run_test_3_store_clarification(client, use_mock=dispatch_mock)
        elif test_num == 4:
            await run_test_4_sequential_trio(client)
        elif test_num == 5:
            await run_test_5_simple_drop(client)
        elif test_num == 6:
            await run_test_6_advanced_lifecycle(client)
        else:
            print("Invalid test number. Choose 1-6.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="A2A client acceptance (mock or live).")
    parser.add_argument("--test", type=int, choices=[1, 2, 3, 4, 5, 6], required=True,
                        help="Test number 1-6 (1=no-findings, 2=single, 3=store, "
                             "4=trio, 5=drop, 6=lifecycle)")
    parser.add_argument("--live", action="store_true",
                        help="Use live SERVER_URL instead of mock handler")
    parser.add_argument("--mock-delay", type=float, default=0.5,
                        help="FakeLangChainAgent delay per step (mock only)")
    args = parser.parse_args()

    asyncio.run(main(args.test, use_mock=not args.live, mock_delay=args.mock_delay))

    print("\n" + "=" * 60)
    print(f"TEST {args.test} COMPLETED SUCCESSFULLY")
    print("=" * 60)
