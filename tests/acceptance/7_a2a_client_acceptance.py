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
# FLAW OF THE TESTS
# they works with 1 filesystem, hence situations of accumulating report files, that agent start seeing in further runs
# means it would keep seeing them and can get confused.

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
# TEST 5: cancel semantics (old 5-simple, redesigned 2026-09-18)
# Design: fire 5, let 1 & 2 stream to completion, cancel 3/4/5 via API and
# verify CANCELED. (The old disconnect-based drop is invalid under the lock
# model — dropped client streams do NOT stop server tasks.)
# ------------------------------------------------------------------
async def run_test_5_simple_drop(client: A2AClient):
    print("\n" + "=" * 60)
    print("[TEST 5] Cancel Semantics (redesigned): 1 & 2 finish, 3/4/5 cancelled")
    print("=" * 60)
    service = client

    print("\n[PHASE 1] Firing 5 tasks (fast IDs via single send)...")
    task_ids = {}
    for i in range(1, 6):
        text = f"Task {i}: Quick summary of recon phase."
        print(f"[TEST5 SENT Task {i}]: {text}")
        tid, _cid, _first = await service.send_message(text, label=f"  [Task {i} send] ")
        task_ids[i] = tid
        print(f"  -> Task {i} created. Server ID: {tid}")

    async def consume(task_id, num):
        try:
            _tid, _cid, artifact = await service.subscribe_to_task(
                task_id, label=f"  [Task {num} sub] "
            )
            print(f"[TEST5 RECEIVED Task {num}]: {(artifact[:200] if artifact else None)}")
            return artifact
        except asyncio.CancelledError:
            print(f"  [Task {num}] subscriber dropped (Cancelled)")
            raise
        except Exception as e:
            print(f"  [Task {num}] subscribe ended: {type(e).__name__}: {e}")
            return None

    print("\n[PHASE 2] Streaming tasks 1 & 2 to completion in background...")
    bg = {i: asyncio.create_task(consume(task_ids[i], i)) for i in (1, 2)}

    print("\n[PHASE 3] Checking state of 3/4/5, then cancelling via API...")
    from a2a.types.a2a_pb2 import TaskState as _TS
    for i in (3, 4, 5):
        data = await service.get_task(task_ids[i], label="  ")
        print(f"  -> Task {i} observed state before cancel: {int(data.status.state)}")
        cancelled = await service.cancel_task(task_ids[i], label="  ")
        print(f"  -> Task {i} state right after cancel: {int(cancelled.status.state)}")

    print("\n[PHASE 4] Verifying 3/4/5 read back CANCELED...")
    cancelled_ok = True
    for i in (3, 4, 5):
        data = await service.get_task(task_ids[i], label="  ")
        ok = data.status.state == _TS.TASK_STATE_CANCELED
        cancelled_ok &= bool(ok)
        print(f"  -> Task {i} final state: {int(data.status.state)} "
              f"({'CANCELED ok' if ok else 'NOT CANCELED'})")

    print("\n[PHASE 5] Letting tasks 1 & 2 finish...")
    for i in (1, 2):
        try:
            await bg[i]
        except asyncio.CancelledError:
            print(f"  -> Task {i} subscriber was cancelled unexpectedly.")
            continue
        print(f"  -> Task {i} finished normally.")

    print("\n[TEST 5 COMPLETE] "
          f"({'all cancels verified' if cancelled_ok else 'CANCEL VERIFICATION FAILED'})")


# RESULT (2026-09-18): redesigned around cancel_task; mock PASS (3/3 CANCELED
# verified, 1 & 2 finished). Live pending.



# ------------------------------------------------------------------
# TEST 6: advanced lifecycle (old 5, redesigned 2026-09-18)
# Design: 1 & 2 stream in background; 3/4/5 launch via single send. Then:
# check-then-cancel 3, subscribe 4 to completion, poll 5 + retrieve. (The old
# "drop subscribers to kill tasks" phases are gone — disconnects do not stop
# server tasks under the lock model.)
# ------------------------------------------------------------------
async def run_test_6_advanced_lifecycle(client: A2AClient):
    print("\n" + "=" * 60)
    print("[TEST 6] Advanced Lifecycle: stream, cancel, subscribe & poll")
    print("=" * 60)
    service = client

    async def stream_bg(num, text):
        print(f"  [SENT Task {num} (streaming)]: {text}")
        try:
            tid, _cid, artifact = await service.send_streaming_message(
                text, label=f"  [Task {num} stream] "
            )
            print(f"  -> Task {num} streamed to completion. Server ID: {tid}")
            return tid, artifact
        except asyncio.CancelledError:
            print(f"  [Task {num}] streaming dropped (Cancelled)")
            raise
        except Exception as e:
            print(f"  [Task {num}] streaming ended: {type(e).__name__}: {e}")
            return None, None

    print("\n[PHASE 1] Streaming tasks 1 & 2 in background...")
    bg = {
        1: asyncio.create_task(stream_bg(1, "Task 1: Generate a quick recon summary.")),
        2: asyncio.create_task(stream_bg(2, "Task 2: Generate a quick recon summary.")),
    }

    print("\n[PHASE 2] Firing 3/4/5 via single send (fast IDs)...")
    task_ids = {}
    for i in (3, 4, 5):
        text = f"Task {i}: Generate a quick recon summary."
        print(f"  [SENT Task {i} (single)]: {text}")
        tid, _cid, _first = await service.send_message(text, label=f"  [Task {i} send] ")
        task_ids[i] = tid
        print(f"  -> Task {i} created. Server ID: {tid}")

    print("\n[PHASE 3] Task 3: check state, then cancel...")
    task3_data = await service.get_task(task_ids[3], label="  ")
    print(f"  -> Task 3 observed state: {int(task3_data.status.state)}")
    await service.cancel_task(task_ids[3], label="  ")
    task3_final = await service.get_task(task_ids[3], label="  ")
    print(f"  -> Task 3 final state: {int(task3_final.status.state)}")

    print("\n[PHASE 4] Task 4: subscribe to completion...")
    task4_data = await service.get_task(task_ids[4], label="  [Get 4] ")
    if task4_data.status.state == TaskState.TASK_STATE_WORKING:
        print("  -> Task 4 is still working. subscribing...")
        try:
            await service.subscribe_to_task(task_ids[4], label="  [Subscribe 4] ")
        except Exception as e:
            print(f"  -> Subscribe ended: {type(e).__name__}: {e}")
    else:
        print(f"  -> Task 4 already terminal: {task4_data.status.state}, skip subscribe")

    print("\n[PHASE 5] Task 5: poll to completion, then retrieve...")
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

    print("\n[PHASE 6] Letting background tasks 1 & 2 finish...")
    for i in (1, 2):
        try:
            await bg[i]
        except asyncio.CancelledError:
            print(f"  -> Task {i} streaming was cancelled unexpectedly.")
            continue
        print(f"  -> Task {i} finished normally.")

    print("\n[TEST 6 COMPLETE]")


# RESULT (2026-09-18): redesigned (no drop-kill phases; 1&2 stream, 3 cancel,
# 4 subscribe, 5 poll); mock PASS all phases. Live pending.


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
