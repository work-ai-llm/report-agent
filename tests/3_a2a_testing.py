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
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest

load_dotenv()

# Use your local network IP where the server is running
SERVER_URL = 'http://192.168.0.107:9999'

# Import store CLI functions directly from your existing tool
from store_cli import seed_store, inspect_store, delete_file


# ==========================================
# 1. CORE HELPER FUNCTION
# ==========================================
async def send_and_get_response(client, text, context_id=None):
    """
    Sends a message, generates Context ID on client side if not provided,
    and extracts the response.
    """
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
    chunk_count = 0

    async for chunk in client.send_message(request):
        """try:
            if chunk.task.context_id:
                returned_context_id = chunk.task.context_id
        except Exception:
            pass

        # 1. Initial Task Object (Sent once at the start)
        if getattr(chunk, 'task', None):
            returned_context_id = chunk.task.context_id
            if chunk.task.status and chunk.task.status.message:
                for p in chunk.task.status.message.parts:
                    if p.text: full_response_text += p.text + " "

        # 2. Intermediate Status Updates (e.g., "Agent is calling tools...")
        elif getattr(chunk, 'status_update', None):
            event = chunk.status_update
            returned_context_id = event.context_id
            if event.status and event.status.message:
                for p in event.status.message.parts:
                    if p.text: 
                        print(f"[STATUS]: {p.text}")
                        full_response_text += p.text + " "

        # 3. Final Artifact Updates (The actual report text)
        elif getattr(chunk, 'artifact_update', None):
            event = chunk.artifact_update
            returned_context_id = event.context_id
            if event.artifact:
                for p in event.artifact.parts:
                    if p.text: 
                        print(f"[ARTIFACT]: {p.text[:100]}...")
                        full_response_text += p.text + " "
                        """
        chunk_count += 1
        print("\n" + "-"*50)
        print(f"CHUNK #{chunk_count} RECEIVED")
        
        # Print the exact Python type (e.g., Pydantic model, Protobuf, dict)
        print(f"TYPE: {type(chunk)}")
        
        # Print the raw object representation
        print(f"RAW OBJECT: {chunk}")
        
        # Print available attributes so we can see what fields actually exist
        attrs = [attr for attr in dir(chunk) if not attr.startswith('_')]
        print(f"ATTRIBUTES: {attrs}")

    print("\n" + "-"*50)
    if chunk_count == 0:
        print("RESULT: The client received ZERO chunks. The stream is empty.")
    else:
        print(f"RESULT: Stream finished. Total chunks received: {chunk_count}")

    print("----------------------")
    print(f"[AGENT RESPONSE]: {full_response_text.strip()}")
    return returned_context_id, full_response_text.strip()

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
    # Based on your seed_store logic, keys are prefixed with /
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
    print("Since streaming=False, the server will block each HTTP request until done.")
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
        if test_num == 1:
            await run_test_1(client)
        elif test_num == 2:
            await run_test_2(client)
        elif test_num == 3:
            await run_test_3(client)
        else:
            print("Invalid test number. Choose 1, 2, or 3.")

        await client.close()




    
if __name__ == "__main__":
    # Professional CLI setup: python 3_a2a_testing.py --test 1
    parser = argparse.ArgumentParser(description="Run specific A2A integration tests.")
    parser.add_argument("--test", type=int, choices=[0, 1, 2, 3], required=True, help="The test number to run (0, 1, 2, or 3)")
    args = parser.parse_args()
    
    asyncio.run(main(args.test))
    
    print("\n" + "="*60)
    print(f"TEST {args.test} COMPLETED SUCCESSFULLY")
    print("="*60)