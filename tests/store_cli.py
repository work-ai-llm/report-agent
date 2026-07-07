import argparse
import datetime
import sys
from pathlib import Path

# Adjust path to import the store and agent
sys.path.append(str(Path(__file__).parent.parent))
from agent.report_agent import langchain_agent, DB_URI
# 1. Import ONLY the sync pool and store
from psycopg_pool import ConnectionPool
from langgraph.store.postgres import PostgresStore

# 2. Initialize the sync pool and store DIRECTLY in the CLI
print("Connecting to Postgres for CLI...")
cli_pool = ConnectionPool(
    conninfo=DB_URI,
    kwargs={"autocommit": True, "prepare_threshold": 0}
)
store = PostgresStore(cli_pool)
store.setup()
print("Store initialized successfully.\n")


# NOTE, let's call this whole file as a stupid way to debug store db.

def seed_store(local_dir: Path):
    print(f"Seeding virtual filesystem from {local_dir}...")
    if not local_dir.exists():
        print(f"Error: Local context folder not found at {local_dir}")
        return

    namespace = ("filesystem",)
    now = datetime.datetime.now().isoformat()
    count = 0

    for file_path in local_dir.rglob("*"):
        if file_path.is_file():
            relative_path = file_path.relative_to(local_dir)
            content = file_path.read_text(encoding="utf-8")
            
            # Ensure leading slash for consistency with FilesystemMiddleware
            file_key = "/" + relative_path.as_posix() 
            
            file_data = {
                "content": content, 
                "encoding": "utf-8",
                "created_at": now,
                "modified_at": now,
            }
            
            store.put(namespace, file_key, file_data)
            print(f"  Seeded: {file_key} ({len(content)} chars)")
            count += 1
            
    print(f"Seeding complete. {count} files added.\n")

def wipe_store(namespace_str: str):
    print(f"Wiping store for namespace: '{namespace_str}'...")
    ns_tuple = tuple(namespace_str.split(".")) if namespace_str else ()
    
    items = store.search(ns_tuple)
    if not items:
        print("No items found to delete.")
        return
        
    for item in items:
        store.delete(item.namespace, item.key)
        print(f"  Deleted: {item.namespace} -> {item.key}")
        
    print(f"Store wiped. {len(items)} items deleted.\n")

def inspect_store(namespace_str: str, show_size: bool, show_dates: bool, preview_chars: int):
    print(f"Inspecting namespace: '{namespace_str}'...")
    ns_tuple = tuple(namespace_str.split(".")) if namespace_str else ()
    
    items = store.search(ns_tuple) 
    if not items:
        print("No items found in this namespace.")
        return
        
    for item in items:
        # Always print the path
        print(f"Path: {item.key}")
        
        # Only process content if we need to show extra details
        if show_size or show_dates or preview_chars > 0:
            content = item.value.get("content", "")
            if isinstance(content, list):
                content = "\n".join(content)
                
            details = []
            if show_size:
                details.append(f"Size: {len(content)} chars")
            if show_dates:
                created = item.value.get("created_at", "N/A")
                modified = item.value.get("modified_at", "N/A")
                details.append(f"Created: {created} | Modified: {modified}")
                
            if details:
                print(f"  {' | '.join(details)}")
                
            if preview_chars > 0:
                preview = content[:preview_chars].replace('\n', ' ')
                print(f"  Preview: {preview}...")
        print("") # Empty line for readability


def inspect_session_store(thread_id: str):
    print(f"Inspecting session state for thread_id: '{thread_id}'...")
    config = {"configurable": {"thread_id": thread_id}}
    
    try:
        state = langchain_agent.get_state(config)
    except Exception as e:
        print(f"Error fetching state: {e}")
        return
        
    if not state or not state.values:
        print("No state found for this thread.")
        return
        
    print(f"Available state keys: {list(state.values.keys())}")
    
    if 'files' in state.values:
        session_files = state.values['files']
        if isinstance(session_files, dict):
            for file_path, file_content in session_files.items():
                print(f"\nPath: {file_path}")
                content = file_content.get("content", "") if isinstance(file_content, dict) else str(file_content)
                size = len(content)
                print(f"  Size: {size} chars")
                preview = content[:100].replace('\n', ' ')
                print(f"  Preview: {preview}...")
        else:
            print("Session files format is unexpected.")
    else:
        print("No session files found in state.")

def export_file(store_path: str, local_path: Path):
    print(f"Exporting '{store_path}' to '{local_path}'...")
    item = store.get(("filesystem",), store_path)
    if not item:
        print(f"Error: File not found at '{store_path}' in the store.")
        return
        
    content = item.value.get("content", "")
    if isinstance(content, list):
        content = "\n".join(content)
        
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(content, encoding="utf-8")
    print(f"Successfully exported to {local_path.absolute()} ({len(content)} chars)\n")

def delete_file(store_path: str):
    print(f"Deleting '{store_path}' from store...")
    namespace = ("filesystem",)
    try:
        store.delete(namespace, store_path)
        print(f"Successfully deleted '{store_path}'.\n")
    except Exception as e:
        print(f"Error deleting file: {e}\n")

def main():
    examples = """
Examples:
  # Wipe the entire store
  python store_cli.py wipe --namespace all

  # Seed the store from a local directory
  python store_cli.py seed --dir ./context

  # Inspect the store (simple list)
  python store_cli.py inspect

  # Inspect the store with metadata and preview
  python store_cli.py inspect --show-size --show-dates --preview 50

  # Export a file from the store to local disk
  python store_cli.py export --store-path /context/templates/recon.md --local-path ./exported_test.md

  # Delete a specific file from the store
  python store_cli.py delete --store-path /context/templates/recon.md

  # inspect store in session
  python store_cli.py inspect-session --thread-id 737280d4-4a28-4427-b851-de581174c3eb
"""

    parser = argparse.ArgumentParser(
        description="LangChain PostgresStore Debugging CLI",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # 1. Seed
    p_seed = subparsers.add_parser("seed", help="Seed the store from a local directory")
    p_seed.add_argument("--dir", type=Path, default=Path("./context"), help="Local directory to seed from")

    # 2. Wipe
    p_wipe = subparsers.add_parser("wipe", help="Wipe the store")
    p_wipe.add_argument("--namespace", type=str, default="filesystem", help="Namespace to wipe (e.g., 'filesystem'). Use 'all' for root.")

    # 3. Inspect
    p_inspect = subparsers.add_parser("inspect", help="Inspect the store contents")
    p_inspect.add_argument("--namespace", type=str, default="filesystem", help="Namespace to inspect")
    p_inspect.add_argument("--show-size", action="store_true", help="Show file sizes")
    p_inspect.add_argument("--show-dates", action="store_true", help="Show creation/modification dates")
    p_inspect.add_argument("--preview", type=int, default=0, help="Number of characters for file preview (0 to disable)")

    # 4. Export
    p_export = subparsers.add_parser("export", help="Export a file from the store to local disk")
    p_export.add_argument("--store-path", type=str, required=True, help="Path in the store (e.g., /context/reports/report.md)")
    p_export.add_argument("--local-path", type=Path, required=True, help="Local path to save to")

    # 5. Delete
    p_delete = subparsers.add_parser("delete", help="Delete a specific file from the store")
    p_delete.add_argument("--store-path", type=str, required=True, help="Path in the store to delete")

    # 6. Inspect Session
    p_session = subparsers.add_parser("inspect-session", help="Inspect session-tied scratchpad files")
    p_session.add_argument("--thread-id", type=str, required=True, help="The thread_id (context_id) of the session")

    args = parser.parse_args()

    if args.command == "seed":
        seed_store(args.dir)
    elif args.command == "wipe":
        ns = "" if args.namespace.lower() == "all" else args.namespace
        wipe_store(ns)
    elif args.command == "inspect":
        # FIX: Pass all the parsed arguments to the function
        inspect_store(args.namespace, args.show_size, args.show_dates, args.preview)
    elif args.command == "export":
        export_file(args.store_path, args.local_path)
    elif args.command == "delete":
        delete_file(args.store_path)
    elif args.command == "inspect-session":
        inspect_session_store(args.thread_id)
    else:
        parser.print_help()

if __name__ == "__main__":
    try:
        main()
    finally:
        # 3. Clean up the pool when the CLI exits
        if cli_pool:
            cli_pool.close()