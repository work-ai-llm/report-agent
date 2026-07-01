import time
import sys
from pathlib import Path

root_dir = Path(__file__).parent.parent
sys.path.insert(0, str(root_dir))


try:
    from agent.report_agent import langchain_agent
except ImportError:
    print("❌ Error: Could not import 'langchain_agent'.")
    print("Please ensure this test file is in the same directory as your agent file,")
    print("and update the import statement if your file is named differently.")
    sys.exit(1)


def resume_interrupted_thread(thread_id: str):
    print(f"Resuming thread: {thread_id}")
    config = {"configurable": {"thread_id": thread_id}}
    
    # PASSING 'None' AS INPUT IS THE MAGIC KEY TO RESUME
    result = langchain_agent.invoke(None, config=config)
    
    print("Resumed successfully!")
    print(result["messages"][-1].content)


def system_prompt_test():
    
    # ==========================================
    # TEST 1: System Prompt Awareness
    # ==========================================
    print("\n" + "=" * 60)
    print("🧪 TEST 1: System Prompt Awareness")
    print("=" * 60)
    
    config_test_1 = {"configurable": {"thread_id": "test-1-identity"}}
    prompt_1 = "Who are you and what is your primary purpose based on your instructions?"
    
    print(f"\n[User]: {prompt_1}")
    print("[Agent]: Thinking...", end="\r")
    
    response_1 = langchain_agent.invoke(
        {"messages": [{"role": "user", "content": prompt_1}]},
        config=config_test_1
    )
    
    # Clear the "Thinking..." line and print the actual response
    print(" " * 20) 
    print(f"[Agent]: {response_1['messages'][-1].content}")
    
    time.sleep(2) # Pause for readability


    # ==========================================
    # TEST 2: In-Memory Persistence (Multi-turn)
    # ==========================================
    print("\n" + "=" * 60)
    print("🧪 TEST 2: In-Memory Persistence (3 Sequential Invokes)")
    print("=" * 60)
    
    # We use a NEW thread_id for this test to keep the memory isolated
    config_test_2 = {"configurable": {"thread_id": "test-2-memory"}}
    
    # --- Turn 1 ---
    prompt_2_1 = "who are you?"
    print(f"\n[Turn 1 - User]: {prompt_2_1}")
    response_2_1 = langchain_agent.invoke(
        {"messages": [{"role": "user", "content": prompt_2_1}]},
        config=config_test_2
    )
    print(f"[Turn 1 - Agent]: {response_2_1['messages'][-1].content}")
    time.sleep(1.5)

    # --- Turn 2 ---
    prompt_2_2 = "what can you do?"
    print(f"\n[Turn 2 - User]: {prompt_2_2}")
    response_2_2 = langchain_agent.invoke(
        {"messages": [{"role": "user", "content": prompt_2_2}]},
        config=config_test_2
    )
    print(f"[Turn 2 - Agent]: {response_2_2['messages'][-1].content}")
    time.sleep(1.5)

    # --- Turn 3 ---
    prompt_2_3 = "what was the 1st question i asked you in this conversation?"
    print(f"\n[Turn 3 - User]: {prompt_2_3}")
    response_2_3 = langchain_agent.invoke(
        {"messages": [{"role": "user", "content": prompt_2_3}]},
        config=config_test_2
    )
    print(f"[Turn 3 - Agent]: {response_2_3['messages'][-1].content}")
    
    time.sleep(1)


def test_3_postgres_sequential():
    """
    Test 3: Verifies sequential memory persistence in Postgres, with local system prompt
    Uses 3 requests with a strict constraint to keep replies to a short paragraph.
    """
    print("\n" + "=" * 60)
    print("🧪 TEST 3: Postgres Sequential Memory (Short Replies)")
    print("=" * 60)
    
    # We use a specific thread_id for this test
    config = {"configurable": {"thread_id": "test-3-postgres-seq"}}
    
    # --- Turn 1 ---
    p1 = "tell me about oneself. Please reply in exactly one short paragraph."
    print(f"\n[Turn 1 - User]: {p1}")
    r1 = langchain_agent.invoke({"messages": [{"role": "user", "content": p1}]}, config=config)
    print(f"[Turn 1 - Agent]: {r1['messages'][-1].content}")
    time.sleep(1.5)

    # --- Turn 2 ---
    p2 = "tell me about your main capabilites. Please reply in exactly one short paragraph."
    print(f"\n[Turn 2 - User]: {p2}")
    r2 = langchain_agent.invoke({"messages": [{"role": "user", "content": p2}]}, config=config)
    print(f"[Turn 2 - Agent]: {r2['messages'][-1].content}")
    time.sleep(1.5)

    # --- Turn 3 ---
    p3 = "Based on our chat, what questions did i ask? Please reply in exactly one short paragraph."
    print(f"\n[Turn 3 - User]: {p3}")
    r3 = langchain_agent.invoke({"messages": [{"role": "user", "content": p3}]}, config=config)
    print(f"[Turn 3 - Agent]: {r3['messages'][-1].content}")
    time.sleep(1.5)


def test_4_session_resumption():
    """
    Test 4: Verifies that an old session can be resumed from Postgres 
    even after interacting with a completely new session.
    """
    print("\n" + "=" * 60)
    print("🧪 TEST 4: Postgres Session Resumption (Old vs New)")
    print("=" * 60)
    
    # ==========================================
    # PART 1: Create a brand new, isolated session
    # ==========================================
    config_new = {"configurable": {"thread_id": "test-4-brand-new-session"}}
    
    p_new = "This is a completely new and separate session. tell me did i ask you anything before? + who are you? Please reply in exactly one short paragraph."
    print(f"\n[New Session - User]: {p_new}")
    r_new = langchain_agent.invoke({"messages": [{"role": "user", "content": p_new}]}, config=config_new)
    print(f"[New Session - Agent]: {r_new['messages'][-1].content}")
    time.sleep(1.5)

    # ==========================================
    # PART 2: Resume the OLD session from Test 3
    # ==========================================
    # Notice we are reusing the EXACT thread_id from Test 3!
    config_old = {"configurable": {"thread_id": "test-3-postgres-seq"}} 
    
    p_old = "We are resuming our old session now. Can you list all the questions I asked you before in this specific session? Please reply in exactly one short paragraph."
    print(f"\n[Resumed Old Session - User]: {p_old}")
    r_old = langchain_agent.invoke({"messages": [{"role": "user", "content": p_old}]}, config=config_old)
    print(f"[Resumed Old Session - Agent]: {r_old['messages'][-1].content}")
    time.sleep(1.5)


def test_5_tool_check():
    """
    Test 4: check whether tool call messages would be seen during conversation 
    """

    config = {"configurable": {"thread_id": "test-6-tool-call"}} 

    # --- turn 1 ---
    p1 = "tell me about yourself, tools you can see and give small evaluation on what you think about it. Please reply in exactly one short paragraph."
    print(f"\n[Turn 1 - User]: {p1}")
    r1 = langchain_agent.invoke({"messages": [{"role": "user", "content": p1}]}, config=config)
    print(f"[Turn 1 - Agent]: {r1['messages'][-1].content}")
    time.sleep(6)

    # --- turn 2 ---
    p1 = "check weather in washington and also tell me your small anaysis of it. Please reply in exactly one short paragraph."
    print(f"\n[Turn 2 - User]: {p1}")
    r1 = langchain_agent.invoke({"messages": [{"role": "user", "content": p1}]}, config=config)
    print(f"[Turn 2 - Agent]: {r1['messages'][-1].content}")
    time.sleep(6)


    p1 = "tell me what weather in paris. Please reply in exactly one short paragraph."
    print(f"\n[Turn 3 - User]: {p1}")
    r1 = langchain_agent.invoke({"messages": [{"role": "user", "content": p1}]}, config=config)
    print(f"[Turn 3 - Agent]: {r1['messages'][-1].content}")
    time.sleep(6)


    p1 = "tell me what i asked you?. Please reply in exactly one short paragraph."
    print(f"\n[Turn 4 - User]: {p1}")
    r1 = langchain_agent.invoke({"messages": [{"role": "user", "content": p1}]}, config=config)
    print(f"[Turn 4 - Agent]: {r1['messages'][-1].content}")
    time.sleep(6)



def test_6_virtual_filesystem_check():
    """
    Test 6: check whether agent sees seeded context files and can write in there 
    """

    config = {"configurable": {"thread_id": "test-6.12-virtual-filesystem"}} 

    # --- turn 1 ---
    p1 = "Can you check what files are available for you, report it to me and also make a small description on each of them"
    print(f"\n[Turn 1 - User]: {p1}")
    r1 = langchain_agent.invoke({"messages": [{"role": "user", "content": p1}]}, config=config)
    print(f"[Turn 1 - Agent]: {r1['messages'][-1].content}")

    # --- turn 2 ---
    p1 = "can you read part of them, and then give me short descrition?"
    print(f"\n[Turn 2 - User]: {p1}")
    r1 = langchain_agent.invoke({"messages": [{"role": "user", "content": p1}]}, config=config)
    print(f"[Turn 2 - Agent]: {r1['messages'][-1].content}")

    # --- turn 3 ---
    p1 = "no. you can read files, read files and then tell me about them, do not make guesses, do not lie you read them"
    print(f"\n[Turn 3 - User]: {p1}")
    r1 = langchain_agent.invoke({"messages": [{"role": "user", "content": p1}]}, config=config)
    print(f"[Turn 3 - Agent]: {r1['messages'][-1].content}")

def test_7_full_report_generation():
    """
    Test 7: Full end-to-end report generation.
    Tests: Virtual FS (ls, read, write), Tool Emulator (list, get, export), 
    Rate Limiter (long duration), and ModelRetryMiddleware.
    """
    config = {"configurable": {"thread_id": "test-7.5-full-report"}}
    
    # These prompts are designed to force sequential, multi-step tool usage
    turns = [
        "Initialize the report generation for the 'recon' phase. First, list the available context files in /context/, read the recon template, and read the recon interpretation guide. Then, list available findings for session 'sess_001', target '192.168.1.50', phase 'recon'.",
        
        "Now, retrieve the 'hosts.md' and 'shares_map.md' from the 'environment_information' layer for that session. Once you have them, start drafting the 'Host Inventory' section of the report based on the template. Write this draft to '/scratchpad/1_host_inventory.md'.",
        
        "Next, retrieve 'forest_domains_trusts.md' from the 'environment_information' layer. Draft the 'Active Directory Infrastructure' section and write it to '/scratchpad/2_ad_infrastructure.md'.",
        
        "Now, retrieve the 'agent_context.md' from the 'agent_context' layer to understand the reasoning. Draft the 'Security Findings' section (make up some realistic mock findings based on the context) and write it to '/scratchpad/3_security_findings.md'.",
        
        "Now that you have written the technical sections in /scratchpad/, read them back one by one. Based on what you wrote, draft an 'Executive Summary' and write it to '/scratchpad/0_executive_summary.md'.",
        
        "Finally, read all the files you just created in /scratchpad/ (the executive summary and the 3 technical sections), concatenate them into a single complete markdown report, and save the final compiled report to '/context/reports/sess_001_recon_final.md' using write_file.",
        
        "Great. Now, use the export tool to save this final report to the local disk for session 'sess_001' and filename 'sess_001_recon_final.md'."
    ]
    
    for i, prompt in enumerate(turns, 1):
        print(f"\n{'='*20} [Turn {i} - User] {'='*20}")
        print(prompt)
        print(f"{'='*60}")
        
        # Invoke the agent
        r = langchain_agent.invoke({"messages": [{"role": "user", "content": prompt}]}, config=config)
        
        print(f"\n[Turn {i} - Agent]:")
        # Print the last AI message content
        print(r['messages'][-1].content)
        print(f"{'='*60}\n")


if __name__ == "__main__":


    print("=" * 60)
    print("🚀 STARTING LANGCHAIN AGENT TESTS")
    print("=" * 60)

    #system_prompt_test()
    #test_3_postgres_sequential()
    #test_4_session_resumption()
    #test_5_tool_check()
    #test_6_virtual_filesystem_check()
    test_7_full_report_generation()
    #resume_interrupted_thread("test-6.10-virtual-filesystem")
    
    # ==========================================
    # TEST COMPLETE
    # ==========================================
    print("\n" + "=" * 60)
    print("✅ ALL TESTS COMPLETED SUCCESSFULLY")
    print("=" * 60)