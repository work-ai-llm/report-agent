import time
import sys
from pathlib import Path

root_dir = Path(__file__).parent.parent
sys.path.insert(0, str(root_dir))


try:
    from agent.test_agent_a2a import langchain_agent
except ImportError:
    print("❌ Error: Could not import 'langchain_agent'.")
    print("Please ensure this test file is in the same directory as your agent file,")
    print("and update the import statement if your file is named differently.")
    sys.exit(1)


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

    config = {"configurable": {"thread_id": "test-5-tool-call"}} 

    # --- turn 1 ---
    p1 = "tell me about yourself, tools you can see and give small evaluation on what you think about it. Please reply in exactly one short paragraph."
    print(f"\n[Turn 1 - User]: {p1}")
    r1 = langchain_agent.invoke({"messages": [{"role": "user", "content": p1}]}, config=config)
    print(f"[Turn 1 - Agent]: {r1['messages'][-1].content}")
    time.sleep(1.5)

    # --- turn 2 ---
    p1 = "check weather in washington and also tell me your small anaysis of it. Please reply in exactly one short paragraph."
    print(f"\n[Turn 2 - User]: {p1}")
    r1 = langchain_agent.invoke({"messages": [{"role": "user", "content": p1}]}, config=config)
    print(f"[Turn 2 - Agent]: {r1['messages'][-1].content}")
    time.sleep(1.5)




if __name__ == "__main__":


    print("=" * 60)
    print("🚀 STARTING LANGCHAIN AGENT TESTS")
    print("=" * 60)

    #system_prompt_test()
    #test_3_postgres_sequential()
    #test_4_session_resumption()
    test_5_tool_check()
    
    # ==========================================
    # TEST COMPLETE
    # ==========================================
    print("\n" + "=" * 60)
    print("✅ ALL TESTS COMPLETED SUCCESSFULLY")
    print("=" * 60)