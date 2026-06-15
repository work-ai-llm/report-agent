import sys
from pathlib import Path
import datetime

# Add the parent directory to the path so we can import from 'agent'
sys.path.append(str(Path(__file__).parent.parent))

from agent.report_agent import store, CONTEXT_DIR

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


def verify_seeded_content(store_instance):
    """Queries the Postgres Store to verify what was actually saved."""
    print("Verifying seeded content in Postgres Store...")
    
    # Search the root "context" namespace
    items = store_instance.search(("context",))
    
    if not items:
        print("WARNING: No items found in the 'context' namespace.")
        return
        
    for item in items:
        # item.namespace is a tuple, e.g., ('context', 'templates')
        ns_str = "/".join(item.namespace)
        print(f"Found: /{ns_str}/{item.key}")
        
        # Print a tiny preview to confirm it's not empty
        content = item.value.get("content", "")
        preview = content[:60].replace('\n', ' ').strip()
        print(f"  Preview: '{preview}...'")
        
    print("Verification complete.")

if __name__ == "__main__":
    seed_context_to_store(store, CONTEXT_DIR)
    #wipe_entire_store(store)
    verify_seeded_content(store)