import sys
import json
from pathlib import Path

root_dir = Path(__file__).parent.parent
sys.path.insert(0, str(root_dir))

from agent.report_agent import langchain_agent
from langchain_core.messages import RemoveMessage

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




def pop_and_retry(thread_id: str):
    config = {"configurable": {"thread_id": thread_id}}
    
    # 1. Get the current state
    state = langchain_agent.get_state(config)
    
    # 2. Find the ID of the message you want to remove (e.g., the last AI message that crashed)
    # Let's say we want to remove the last message in the history
    last_msg_id = state.values["messages"][-1].id
    
    # 3. Update the state by "removing" that message
    langchain_agent.update_state(
        config, 
        {"messages": [RemoveMessage(id=last_msg_id)]}
    )
    
    print("Message popped! Resuming...")


if __name__ == "__main__":
    #inspect_thread("test-3-postgres-seq")
    #inspect_thread("test-4-brand-new-session")
    #inspect_thread("test-5-tool-call")
    #inspect_thread("0df1f9b6-87c7-40e0-a163-73cd201a3a36")
    #inspect_thread("test-6-virtual-filesystem")
    #inspect_thread("test-6.1-virtual-filesystem") # unsuccessful fix of virtual filesystem
    #inspect_thread("test-6.2-virtual-filesystem") #tool emulator worked
    #inspect_thread("test-6.3-virtual-filesystem") # namespace wasn't the issue
    #inspect_thread("test-6.4-virtual-filesystem") # namespace issue not resolved, maybe putting had to be different
    #inspect_thread("test-6.5-virtual-filesystem") # depreciated warning is FUCKING annoying
    #inspect_thread("test-6.6-virtual-filesystem") # reading files failed
    #inspect_thread("test-6.7-virtual-filesystem") # reading didn't fail, wiping db got rid of wrong files, still depreciated though
    #inspect_thread("test-6.8-virtual-filesystem") # timeout increase, resume interrupted session
    #inspect_thread("test-6.9-virtual-filesystem") # modelretry middlewear, streaming addition + popping the last message in session, still readtimout error
    #pop_and_retry("test-6.9-virtual-filesystem")
    #inspect_thread("test-6.10-virtual-filesystem") # tool call interruption corrupts the whole thread?
    #inspect_thread("test-6.11-virtual-filesystem") #stopped because model invoke stopped working
    #inspect_thread("test-6.12-virtual-filesystem") # this one worked by turning off rate limiter and model retry
    #inspect_thread("test-7-full-report") # had an issue of readtimeout, want to inspect more detailed logs
    #inspect_thread("test-7.1-full-report") # timeout with logging, was too little
    #inspect_thread("test-7.2-full-report") # tool emulator blocked the api access, rate limiter dropped http client tcp connection, requires custom httpx client, also testing vpn access
    #inspect_thread("test-7.3-full-report") # vpn broke connection
    # starting from this, i need another way of monitoring, cmd output exceeds the cmd window limit already
    inspect_thread("test-7.4-full-report") # worked pretty sweet, the issue was large mistral models was blocked without vpn

