import sys
from pathlib import Path

root_dir = Path(__file__).parent.parent
sys.path.insert(0, str(root_dir))

from agent.test_agent_a2a import langchain_agent

def inspect_thread(thread_id: str):
    print(f"\n--- Inspecting: {thread_id} ---")
    
    # 1. Define the config for the thread you want to look at
    config = {"configurable": {"thread_id": thread_id}}
    
    # 2. The magic built-in method. It handles all the Postgres/msgpack decoding!
    state = langchain_agent.get_state(config)
    
    if not state or not state.values:
        print("No data found.")
        return

    # 3. Extract and print the messages
    # state.values["messages"] contains actual HumanMessage/AIMessage objects!
    messages = state.values.get("messages", [])
    
    for i, msg in enumerate(messages):
        msg_type = msg.type.upper()
        print(f"[{i}] {msg_type}")
        print("-" * 40)
        
        # 1. CHECK FOR HIDDEN TOOL CALL REQUESTS
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            print("RAW TOOL CALL:")
            print(f"     {msg.tool_calls}")
        
        # 2. PRINT THE TEXT CONTENT (if it exists)
        if msg.content:
            print(f"CONTENT:\n     {msg.content}")
            
        # 3. FOR TOOL MESSAGES, SHOW THE LINK
        if msg_type == "TOOL" and hasattr(msg, "tool_call_id"):
            print(f"  🔗 LINKED TO CALL ID: {msg.tool_call_id}")
            
        print() # Empty line for readability


if __name__ == "__main__":
    #inspect_thread("test-3-postgres-seq")
    #inspect_thread("test-4-brand-new-session")
    inspect_thread("test-5-tool-call")