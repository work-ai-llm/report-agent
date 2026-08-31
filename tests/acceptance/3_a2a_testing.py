import argparse
import requests
import uuid
import json
from pathlib import Path
import asyncio
import httpx
from uuid import uuid4
from dotenv import load_dotenv

# Official A2A Client Imports
from a2a.client import A2ACardResolver, ClientConfig, create_client
# (Protobuf-based)
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest, SubscribeToTaskRequest
# for protobuf object conversion
from google.protobuf.json_format import MessageToDict


load_dotenv()

# Use your local network IP where the server is running
SERVER_URL = 'http://192.168.0.107:9999'

# Import store CLI functions directly from your existing tool
from dev_tools.store_cli import seed_store, inspect_store, delete_file

# ==========================================
# proper extraction function
# ==========================================
def extract_chunk_info(chunk):
    """
    Takes the raw Protobuf chunk from the SDK.
    Returns: (text_content, task_id, context_id, chunk_type)
    """
    # 1. Get Metadata directly from Protobuf attributes (No dict conversion needed!)
    # Task object has 'id', while Updates have 'task_id'
    task_id = getattr(chunk, 'task_id', None) or getattr(chunk, 'id', None)
    context_id = getattr(chunk, 'context_id', None)
    
    # 2. Convert to dict ONLY to extract the nested text payloads
    chunk_dict = MessageToDict(chunk, preserving_proto_field_name=True)
    text_content = None
    chunk_type = None

    if "task" in chunk_dict:
        chunk_type = "INITIAL_TASK"
        task_dict = chunk_dict['task']
        status = chunk_dict["task"].get("status", {}).get("state")
        text_content = f"[TASK STATE]: {status}"
        history_list = task_dict.get("history", [])

        for history_item in history_list:
            parts = history_item.get("parts", [])
            for part in parts:
                if "text" in part:                    
                    text_content = f"[TASK STATE]: {status}\nText: {part['text']}"
                    break
        
    elif "status_update" in chunk_dict:
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


# ==========================================
# CORE HELPER FUNCTION
# ==========================================
async def send_and_get_response(client, text, context_id=None, task_id=None):
    """
    Sends a message, generates Context ID on client side if not provided,
    and extracts the response.
    """

    if task_id:
        sub_request = SubscribeToTaskRequest(id=task_id)

        async for chunk in client.subscribe_to_task(task_id=task_id):
            converted = MessageToDict(chunk, preserving_proto_field_name=True)

            extract_text_heuristically(converted)
            print(extract_text_heuristically)



    if not context_id:
        context_id = str(uuid.uuid4())
        print(f">>> CLIENT GENERATED CONTEXT ID: {context_id}")

    # 1. Create the Protobuf Part
    part = Part(text=text)
    
    # 2. Create the Message
    # 2. Create the Protobuf Message
    msg_kwargs = {
        'role': Role.ROLE_USER,
        'parts': [part],
        'message_id': str(uuid.uuid4()),
    }
    if context_id:
        msg_kwargs['context_id'] = context_id

    message = Message(**msg_kwargs)
    
    # 3. Create the Params
    request = SendMessageRequest(
        message=message)

    print(f"\n[CLIENT SENDING]: {text}")
    print("--- WAITING FOR SERVER RESPONSE ---")
    
    returned_context_id = context_id
    full_response_text = ""
    agent_response = ""
    chunk_count = 0

    async for chunk in client.send_message(request):
        chunk_count += 1
        print("\n" + "-"*50)
        #print(f"CHUNK #{chunk_count} RECEIVED")
        
        """# Print the exact Python type (e.g., Pydantic model, Protobuf, dict)
        print(f"TYPE: {type(chunk)}")
        
        # Print the raw object representation
        print(f"RAW OBJECT: {chunk}")
        
        # Print available attributes so we can see what fields actually exist
        attrs = [attr for attr in dir(chunk) if not attr.startswith('_')]
        print(f"ATTRIBUTES: {attrs}")"""
        converted = MessageToDict(chunk,  preserving_proto_field_name=True)

        extracted_text = extract_text_heuristically(converted)

        if "artifact_update" in converted:
            agent_response = extracted_text
        elif extracted_text:
            full_response_text += extracted_text + " "
            print(extracted_text) # Optional: see what was found
        else:
            # Fallback: Print warning if the heuristic failed
            print(f"[WARNING] Chunk extraction didn't work, possible that schema is different. \n"
                f"Raw chunk: {chunk}")

    print("\n" + "-"*50)
    if chunk_count == 0:
        print("RESULT: The client received ZERO chunks. The stream is empty.")
    else:
        print(f"RESULT: Stream finished. Total chunks received: {chunk_count}")

    print("----------------------")
    #print(f"[AGENT RESPONSE]: {full_response_text.strip()}")
    print(f"[AGENT RESPONSE]: {agent_response.strip()}")
    return returned_context_id, full_response_text.strip()


async def stream_to_completion(client, stream, label=""):
    """Consumes a stream (from send_message or subscribe_to_task) until COMPLETED."""
    final_artifact = ""
    async for chunk in stream:
        text, tid, cid, ctype = extract_chunk_info(chunk)
        if text:
            print(f"{label}{text[:100]}")
        if ctype == "ARTIFACT":
            final_artifact = text
        if ctype == "STATUS" and "TASK_STATE_COMPLETED" in text:
            break
    return final_artifact



import uuid
import asyncio
from google.protobuf.json_format import MessageToDict
from a2a.types.a2a_pb2 import (
    Part, Role, Message, SendMessageRequest, 
    GetTaskRequest, CancelTaskRequest, SubscribeToTaskRequest, TaskState
)


# ==========================================
# TEST DEFINITIONS
# ==========================================

async def run_test_0_streaming(client):
    print("\n" + "="*60)
    print("[TEST 0] confirm client streaming")
    print("="*60)
    ctx_id, response = await send_and_get_response(
        client,
        "create a small report for the recon phase based on findings. do not follow assembly workflow."
    )
    print("\n[TEST 0 COMPLETE]")


async def run_test_1(client):
    print("\n" + "="*60)
    print("[TEST 1] Single Task Loop (Verify Scratchpad Workflow)")
    print("="*60)
    ctx_id, response = await send_and_get_response(
        client,
        "Initialize the report generation for the recon phase. Follow the scratchpad assembly workflow."
    )
    print("\n[TEST 1 COMPLETE]")

async def run_test_2(client):
    print("\n" + "="*60)
    print("[TEST 2] Store Injection & Clarification Loop")
    print("="*60)
    
    # 1. Explicitly delete only the finding files, preserving templates/interpretations
    print("\n--- Step 1: Deleting specific finding files from Store ---")
    delete_file("/findings/nmap_scans.txt")
    delete_file("/findings/bloodhound_dump.txt")
    
    # 2. Inspect to confirm they are gone
    print("\n--- Step 2: Inspecting Store to confirm deletion ---")
    inspect_store("filesystem", True, False, 50)
    
    # 3. Ask agent to generate report (it should complain about missing files)
    print("\n--- Step 3: Asking for report without files ---")
    ctx_id, response_t1 = await send_and_get_response(
        client,
        "Generate the pentest report based on the available findings."
    )
    
    # 4. Seed the store with actual findings from the results folder
    print("\n--- Step 4: Seeding Store with findings ---")
    findings_dir = Path(__file__).parent.parent / "results"
    seed_store(findings_dir)
    
    # 5. Inspect to confirm they are back
    print("\n--- Step 5: Inspecting Store to confirm files are uploaded ---")
    inspect_store("filesystem", True, False, 50)
        
    # 6. Tell agent we uploaded the file using the SAME context_id
    print("\n--- Step 6: Informing agent about uploaded file ---")
    ctx_id, response_t2 = await send_and_get_response(
        client,
        "I uploaded the findings to the store now. Can you start the report generation?",
        context_id=ctx_id 
    )
    print("\n[TEST 2 COMPLETE]")


async def run_test_3(client):
    print("\n" + "="*60)
    print("[TEST 3] Back-to-Back Complex Tasks (Concurrency Test)")
    print("="*60)
    print("Firing two complex tasks simultaneously to test server concurrency.")
    print("We use asyncio.gather to fire both at the exact same time.\n")

    task1_text = (
        "Generate a comprehensive executive summary report analyzing all network vulnerabilities "
        "found in the nmap scans. Provide detailed remediation steps for each critical finding."
    )
    
    task2_text = (
        "Analyze the Active Directory bloodhound dump. Identify all critical privilege escalation paths "
        "and provide a step-by-step mitigation plan for the domain controller."
    )

    # Fire both tasks concurrently to stress-test the server
    results = await asyncio.gather(
        send_and_get_response(client, task1_text),
        send_and_get_response(client, task2_text)
    )
    
    ctx1, resp1 = results[0]
    ctx2, resp2 = results[1]
    
    print("\n--- CONCURRENCY TEST RESULTS ---")
    print(f"Task 1 Context ID: {ctx1}")
    print(f"Task 2 Context ID: {ctx2}")
    print("Check Langfuse to see if these were processed concurrently or queued.")
    print("\n[TEST 3 COMPLETE]")

async def run_test_4(client):
    print("\n" + "="*60)
    print("[TEST 4] Sequential Execution Stress Test (Multiple Concurrent Tasks)")
    print("="*60)
    print("Firing 3 complex tasks simultaneously from the client.")
    print("The server should queue them and execute them strictly one by one.")
    print("Check the server logs to see them being picked up sequentially.\n")

    tasks = [
        "Generate a quick summary of the recon phase.",
        "Analyze the bloodhound dump for privilege escalation paths.",
        "Create a draft for the executive summary based on nmap scans.",
    ]

    # Fire all 3 concurrently
    results = await asyncio.gather(
        *[send_and_get_response(client, text) for text in tasks]
    )
    
    for i, (ctx, resp) in enumerate(results):
        print(f"\nTask {i+1} Context ID: {ctx}")
        # Just print a snippet to avoid flooding the console
        snippet = resp[:150].replace('\n', ' ') if resp else "No response"
        print(f"Task {i+1} Response snippet: {snippet}...")

    print("\n[TEST 4 COMPLETE]")


# ==========================================
# TEST 5: Concurrency & Client Drop (Refactored)
# ==========================================
async def run_test_5(client):
    print("\n" + "="*60)
    print("[TEST 5] Concurrency Limit & Client Drop Simulation")
    print("="*60)
    print("Firing 5 tasks simultaneously.")
    print("Tasks 1 & 2 will finish. Tasks 3, 4, 5 will be dropped after 3 seconds.\n")

    # Helper to run a single stream using YOUR exact function

    async def run_single_stream(task_num, text):
        ctx_id = str(uuid.uuid4())
        try:
            # Call your exact function. It handles all the printing and parsing.
            returned_ctx, response_text = await send_and_get_response(client, text, context_id=ctx_id)
            print(f"[Task {task_num:02d}] Finished normally.")
            return returned_ctx, response_text
        except asyncio.CancelledError:
            # This triggers when we call task.cancel() below
            print(f"[Task {task_num:02d}] !!! CLIENT CONNECTION DROPPED (Cancelled) !!!")
            raise
        except Exception as e:
            print(f"[Task {task_num:02d}] Stream ended with error: {type(e).__name__}")
            return ctx_id, None

    all_tasks = []

    # PHASE 1: Fire tasks using create_task (non-blocking)
    for i in range(5):
        text = f"Task {i+1}: Quick summary of recon phase."
        task_coro = run_single_stream(i + 1, text)
        all_tasks.append(asyncio.create_task(task_coro))

    # PHASE 2: Let the queue build up
    print("\n--- WAITING 5 SECONDS TO LET QUEUE BUILD UP ---")
    await asyncio.sleep(5)

    # PHASE 3: Drop the clients for tasks 3, 4, 5
    print("\n--- DROPPING CLIENT CONNECTIONS FOR TASKS 3, 4, 5 ---")
    for i in range(2, 5):
        all_tasks[i].cancel()

    # PHASE 4: Wait for completion (return_exceptions prevents crash on CancelledError)
    print("\n--- WAITING FOR TASKS 1 & 2 TO COMPLETE ---")
    results = await asyncio.gather(*all_tasks, return_exceptions=True)

    # PHASE 5: Summary
    print("\n" + "-"*50)
    print("--- TEST 5 RESULTS ---")
    for i, res in enumerate(results):
        if isinstance(res, asyncio.CancelledError):
            print(f"Task {i+1}: DROPPED (Client disconnected)")
        elif isinstance(res, Exception):
            print(f"Task {i+1}: FAILED with {type(res).__name__}")
        else:
            ctx, _ = res
            print(f"Task {i+1}: COMPLETED (Context: {ctx[:8]}...)")
            
    print("\n[TEST 5 COMPLETE] (Check server logs for 'Skipping task...')")


from a2a.types.a2a_pb2 import SubscribeToTaskRequest # Import the specific request type

# ==========================================
# TEST 5: Advanced Lifecycle (Drop, Zombie, Subscribe & Get)
# ==========================================
async def run_test_5(client):
    print("\n" + "="*60)
    print("[TEST 5] Advanced Lifecycle: Drop, Zombie, Subscribe & Get")
    print("="*60)

    # 1. Initialize the Service
    service = A2ATaskService(client)

    # --- PHASE 1: FIRE 5 TASKS & GET IDS ---
    print("\n[PHASE 1] Firing 5 tasks and extracting Server Task IDs...")
    active_tasks = {}
    task_ids = {}
    
    for i in range(1, 6):
        text = f"Task {i}: Generate a quick recon summary."
        stream, tid, cid = await service.start_stream(text)
        task_ids[i] = tid
        print(f"  -> Task {i} created. Server ID: {tid}")
        
        # Wrap the rest of the stream in a background task so we can drop it later
        async def consume(s, num):
            await service._process_stream(s, label=f"  [Task {num}] ")
            
        active_tasks[i] = asyncio.create_task(consume(stream, i))

    # --- PHASE 2: DROP 3, 4, 5 ---
    print("\n[PHASE 2] Letting server process for 5 seconds, then dropping 3, 4, 5...")
    await asyncio.sleep(5)
    
    for i in [3, 4, 5]:
        active_tasks[i].cancel() # Violently sever the TCP connection
        
    # --- PHASE 3: LET 1 & 2 FINISH ---
    print("\n[PHASE 3] Letting Tasks 1 & 2 finish normally...")
    for i in [1, 2]:
        await active_tasks[i]
        print(f"  -> Task {i} finished normally.")

    # Wait for the server to process the dropped tasks in the background (Zombie mode)
    print("\n[PHASE 4] Waiting 10 seconds for server to process 'zombie' tasks...")
    await asyncio.sleep(10)

    # --- PHASE 5: TASK 3 (Check -> Subscribe) ---
    print("\n[PHASE 5] Handling Task 3 (Check status, then cancel)...")
    task3_data = await service.get_task(task_ids[3], label="  ")
    if task3_data.status.state == TaskState.TASK_STATE_WORKING:
        print("  -> Task 3 is still working. cancel")
        await service.cancel_task(task_ids[3], label="  ")
    else:
        print("  -> Task 3 canceled")

    # --- PHASE 6: TASK 4 (check status -> Get & Replay) ---
    print("\n[PHASE 6] Handling Task 4 ( Get & Subscribe)...")
    await service.get_task(task_ids[4], label="  [Get 4] ")
    print("  -> Task 4 stream via subscribe_to_task...")
    if task3_data.status.state == TaskState.TASK_STATE_WORKING:
        print("  -> Task 4 is still working. cancel")
        await service.subscribe_to_task(task_ids[4], label="  [Subscribe 4] ")
    else:
        print("  -> Task 4 dropped")
    

    # --- PHASE 7: TASK 5 (Poll -> Wait -> Get & Replay) ---
    print("\n[PHASE 7] Handling Task 5 (Polling get_task, then Retrieve)...")
    # Polling every 60 seconds for 10 times (10 minutes total)
    task5_data = await service.poll_until_complete(task_ids[5], interval=60, max_attempts=10, label="  ")
    
    if task5_data and task5_data.status.state == TaskState.TASK_STATE_COMPLETED:
        print("  -> Task 5 completed. Starting 10-minute countdown to check if it disappears...")
        for i in range(10):
            print(f"  -> Countdown check {i+1}/10...")
            await asyncio.sleep(60)
            check = await service.get_task(task_ids[5], label="     ")
            if check.status.state != TaskState.TASK_STATE_COMPLETED:
                print("  -> WARNING: Task disappeared or changed state!")
                break
                
        print("  -> Countdown passed. Retrieving Task 5 final payload...")
        await service.get_task(task_ids[5], label="  [Get 5] ")
        await service.subscribe_to_task(task_ids[5], label="  [Replay 5] ")
    else:
        print("  -> Task 5 did not complete in time!")

    print("\n[TEST 5 COMPLETE]")


# ==========================================
# MAIN EXECUTION & CLI
# ==========================================
async def main(test_num):
    print("\n" + "="*60)
    print(f"STARTING A2A INTEGRATION TEST {test_num}")
    print("="*60)

    async with httpx.AsyncClient(timeout=300.0) as httpx_client:
        resolver = A2ACardResolver(httpx_client=httpx_client, base_url=SERVER_URL)
        public_agent_card = await resolver.get_agent_card()
        print(f"Found agent: {public_agent_card.name}")

        config = ClientConfig(streaming=True, httpx_client=httpx_client)
        client = await create_client(agent=public_agent_card, client_config=config)


        if test_num == 0:
            await run_test_0_streaming(client)
        elif test_num == 1:
            await run_test_1(client)
        elif test_num == 2:
            await run_test_2(client)
        elif test_num == 3:
            await run_test_3(client)
        elif test_num == 4:
            await run_test_4(client)
        elif test_num == 5:
            await run_test_5(client)
        else:
            print("Invalid test number. Choose 0, 1, 2, 3, 4 or 5.")

        await client.close()

if __name__ == "__main__":
    # Professional CLI setup: python 3_a2a_testing.py --test 1
    parser = argparse.ArgumentParser(description="Run specific A2A integration tests.")
    parser.add_argument("--test", type=int, choices=[0, 1, 2, 3, 4, 5], required=True, help="The test number to run (0, 1, 2, 3, 4 or 5)")
    args = parser.parse_args()
    
    asyncio.run(main(args.test))
    
    print("\n" + "="*60)
    print(f"TEST {args.test} COMPLETED SUCCESSFULLY")
    print("="*60)
