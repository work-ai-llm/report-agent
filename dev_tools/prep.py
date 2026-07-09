# tool description
# initial way set up virtual filesystem for report agent
# initial setup preparation (require remaking)

import sys
from pathlib import Path
import datetime

# Add the parent directory to the path so we can import from 'agent'
sys.path.append(str(Path(__file__).parent.parent))

from agent.report_agent import store, langchain_agent

def seed_context_to_store(store_instance, local_context_dir: Path):
    print("Seeding virtual filesystem from local context...")
    
    if not local_context_dir.exists():
        print(f"Local context folder not found. Aborting.")
        return

    # 1. The official tests hardcode the namespace to ("filesystem",) for StoreBackend
    namespace = ("filesystem",)
    
    # 2. Timestamps to match the FileData schema expected by StoreBackend
    now = datetime.datetime.now().isoformat()

    for file_path in local_context_dir.rglob("*.md"):
        relative_path = file_path.relative_to(local_context_dir)
        content = file_path.read_text(encoding="utf-8")
        
        # 3. The key must be the full path with a leading slash
        file_key = "/" + relative_path.as_posix() 
        
        # 4. EXACT schema used in the official deepagents tests
        # Note: content is a list of strings, and metadata is required!
        file_data = {
            "content": content, 
            "encoding": "utf-8",
            "created_at": now,
            "modified_at": now,
        }
        
        store_instance.put(namespace, file_key, file_data)
        print(f"Seeded: /context{file_key}")
        
    print("Seeding complete.\n")


def wipe_entire_store(store_instance):
    print("Wiping ENTIRE virtual filesystem and storage...")
    
    # Passing an empty tuple () searches the absolute root of the store,
    # catching ("filesystem",), ("context",), ("context", "templates"), etc.
    all_items = store_instance.search(())
    
    for item in all_items:
        # Pass the item's precise namespace tuple and its key to delete it
        store_instance.delete(item.namespace, item.key)
        print(f"Deleted: {item.namespace} -> {item.key}")
        
    print("Store is completely empty.\n")


def inspect_global_store(store_instance):
    """Inspects the PostgresStore (Global '/context/' path)."""
    print("="*60)
    print("1. INSPECTING POSTGRES STORE (Global '/context/')")
    print("="*60)
    
    # Search the namespace defined in your FilesystemMiddleware
    items = store_instance.search(("filesystem",)) 
    
    if not items:
        print("No items found in the ('filesystem',) namespace.")
        return
        
    for item in items:
        print(f"Virtual Path: /context/{item.key}")
        print(f"Raw Namespace: {item.namespace}")
        
        content = item.value
        if isinstance(content, dict):
            content = content.get("content") or content.get("text") or str(content)
            
        preview = str(content)[:100].replace('\n', ' ')
        print(f"Preview: {preview}...\n")


def inspect_session_store(agent, thread_id: str):
    """Inspects the StateBackend (Session-tied '/scratchpad/' path)."""
    print("="*60)
    print("2. INSPECTING STATE BACKEND (Session-tied)")
    print("="*60)
    
    config = {"configurable": {"thread_id": thread_id}}
    state = agent.get_state(config)
    
    if not state or not state.values:
        print("No state found for this thread.")
        return
        
    print(f"Available state keys: {list(state.values.keys())}")
    
    # Check common keys used by FilesystemMiddleware for session files
    found_session_files = False
    for key in ['files', 'filesystem', 'virtual_fs', 'store']:
        if key in state.values:
            found_session_files = True
            print(f"\nFound session files in state key: '{key}'")
            session_files = state.values[key]
            if isinstance(session_files, dict):
                for file_path, file_content in session_files.items():
                    print(f"  Path: {file_path}")
                    preview = str(file_content)[:100].replace('\n', ' ')
                    print(f"  Preview: {preview}...\n")
                    
    if not found_session_files:
        print("No session-specific files found in the state.")

if __name__ == "__main__":
    #seed_context_to_store(store, CONTEXT_DIR)
    #wipe_entire_store(store)
    inspect_global_store(store)
    inspect_session_store(langchain_agent, "737280d4-4a28-4427-b851-de581174c3eb")