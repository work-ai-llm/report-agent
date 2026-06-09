import sys
import json
from pathlib import Path

root_dir = Path(__file__).parent.parent
sys.path.insert(0, str(root_dir))

from agent.report_agent import langchain_agent

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


    for msg in messages:
        print(msg.pretty_repr())


    #print(json.dumps([msg.model_dump() for msg in messages], indent=2))



if __name__ == "__main__":
    #inspect_thread("test-3-postgres-seq")
    #inspect_thread("test-4-brand-new-session")
    #inspect_thread("test-5-tool-call")
    inspect_thread("0df1f9b6-87c7-40e0-a163-73cd201a3a36")