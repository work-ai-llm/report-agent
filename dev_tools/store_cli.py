# tool description
# convinient tool to inspect,delete,add files from pc filesystem to postgress virtual filesystem
# filesystem control

import argparse
import asyncio
import datetime
import sys
from pathlib import Path

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

# Adjust path to import the store and agent
sys.path.append(str(Path(__file__).parent.parent))
from agent.report_agent import runtime

# No pool/store created at import - reused from runtime (same file as AgentRuntime LOCAL_DB_PATH)
# via await runtime._init_pool() in main(). No extra pool.

async def _ensure_runtime_store():
    if runtime.store is None or runtime.checkpointer is None:
        await runtime._init_pool()

async def seed_store(local_dir: Path):
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
            file_key = "/" + relative_path.as_posix() 
            file_data = {
                "content": content, 
                "encoding": "utf-8",
                "created_at": now,
                "modified_at": now,
            }
            await runtime.store.aput(namespace, file_key, file_data)
            print(f"  Seeded: {file_key} ({len(content)} chars)")
            count += 1
    print(f"Seeding complete. {count} files added.\n")

async def wipe_store(namespace_str: str):
    print(f"Wiping virtual filesystem namespace: '{namespace_str}'...")
    ns_tuple = tuple(namespace_str.split(".")) if namespace_str else ()
    items = await runtime.store.asearch(ns_tuple)
    if not items:
        print("No items found to delete.")
        return
    for item in items:
        await runtime.store.adelete(item.namespace, item.key)
        print(f"  Deleted: {item.namespace} -> {item.key}")
    print(f"Store wiped. {len(items)} items deleted.\n")

async def inspect_store(namespace_str: str, show_size: bool, show_dates: bool, preview_chars: int):
    print(f"Inspecting virtual filesystem namespace: '{namespace_str}'...")
    ns_tuple = tuple(namespace_str.split(".")) if namespace_str else ()
    items = await runtime.store.asearch(ns_tuple)
    if not items:
        print("No items found in this namespace.")
        return
    for item in items:
        print(f"Path: {item.key}")
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
        print("")

async def _fetch_thread_ids(max_threads: int) -> list[str]:
    """Run the lightweight DISTINCT thread_id scan on the ACTIVE backend."""
    # Postgres: psycopg async pool, dict_row row factory.
    if runtime.pool is not None:
        async with runtime.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT DISTINCT thread_id FROM checkpoints "
                    "ORDER BY thread_id LIMIT %s",
                    (max_threads,),
                )
                rows = await cur.fetchall()
        return sorted(str(row["thread_id"]) for row in rows)

    # SQLite fallback: the checkpointer's own aiosqlite connection points at
    # the ACTIVE local file (LOCAL_DB_PATH), so no hardcoded path is needed.
    if runtime.checkpointer is not None and hasattr(runtime.checkpointer, "conn"):
        async with runtime.checkpointer.conn.execute(
            "SELECT DISTINCT thread_id FROM checkpoints "
            "ORDER BY thread_id LIMIT ?",
            (max_threads,),
        ) as cur:
            rows = await cur.fetchall()
        return sorted(str(row[0]) for row in rows)

    return []


async def _active_thread_ids(max_threads: int = 5000) -> list[str]:
    """Distinct thread_ids from the ACTIVE checkpointer, failing fast.

    The old implementation read ``LOCAL_DB_PATH`` directly with aiosqlite and
    plain ``SELECT DISTINCT thread_id FROM checkpoints``, which only ever
    reflected the SQLite fallback (stale/empty when the runtime is on Postgres).
    A later attempt used ``checkpointer.alist(None)`` but that is an *unbounded*
    full-table scan of every namespace that deserializes + de-blobs every
    checkpoint row - far too slow to enumerate sessions on a busy Postgres DB
    (that is exactly why ``list-sessions`` hung with a Postgres backend).

    Instead, run the distinct scan directly on the ACTIVE backend and bound the
    wait so a down/slow pool surfaces an error instead of hanging the CLI.
    """
    try:
        return await asyncio.wait_for(_fetch_thread_ids(max_threads), timeout=15.0)
    except asyncio.TimeoutError:
        raise RuntimeError(
            "timed out waiting for a checkpointer connection - is Postgres "
            "reachable? (check your Tailscale/VPN and psql)"
        ) from None

async def list_sessions():
    """List all thread_ids chronologically with created/last timestamps."""
    print("Listing sessions in checkpointer chronologically...", flush=True)
    try:
        tids = await _active_thread_ids()
        if not tids:
            print("No sessions found.")
            return
        sessions = []
        total = len(tids)
        for i, tid in enumerate(tids, 1):
            print(f"  scanning session {i}/{total} ...", flush=True)
            try:
                async def _fetch_ckpts():
                    return [t async for t in runtime.checkpointer.alist(
                        {"configurable": {"thread_id": tid}}, limit=512
                    )]
                checkpoints = await asyncio.wait_for(_fetch_ckpts(), timeout=20.0)
                if not checkpoints:
                    sessions.append((tid, None, None, 0, "?"))
                    continue
                checkpoints = list(reversed(checkpoints))  # chronological
                first_ts = checkpoints[0].checkpoint.get("ts") if isinstance(checkpoints[0].checkpoint, dict) else None
                last_ts = checkpoints[-1].checkpoint.get("ts") if isinstance(checkpoints[-1].checkpoint, dict) else None
                # Fallback to metadata if ts missing
                if not first_ts and checkpoints[0].metadata:
                    first_ts = checkpoints[0].metadata.get("created_at") or checkpoints[0].checkpoint.get("id")
                if not last_ts and checkpoints[-1].metadata:
                    last_ts = checkpoints[-1].metadata.get("updated_at")
                tup = checkpoints[-1]
                vals = tup.checkpoint.get("channel_values", {}) if isinstance(tup.checkpoint, dict) else {}
                msgs = vals.get("messages", [])
                step = tup.metadata.get("step", "?") if tup.metadata else "?"
                sessions.append((tid, first_ts, last_ts, len(msgs), step))
            except Exception as e:
                sessions.append((tid, None, None, 0, f"error:{e}"))
        # Sort chronologically by created ts (ISO strings sort correctly)
        sessions.sort(key=lambda x: x[1] or "")
        def _explain_ts(ts: str) -> str:
            # 2026-08-25T16:05:27.221455+00:00 -> 2026 year - 08 month - 25 day T 16 hour:05 minute:27 second .221455 microseconds +00:00 UTC
            if not ts or ts == "unknown":
                return ""
            try:
                date, time_tz = ts.split("T") if "T" in ts else (ts, "")
                year, month, day = date.split("-") if "-" in date else (date, "", "")
                tz = ""
                if "+" in time_tz:
                    time_part, tz = time_tz.split("+", 1)
                    tz = "+" + tz + " UTC"
                elif time_tz.endswith("Z"):
                    time_part = time_tz[:-1]
                    tz = "Z UTC"
                else:
                    time_part = time_tz
                hour = minute = second = micro = ""
                if ":" in time_part:
                    h, m, rest = time_part.split(":", 2)
                    hour, minute = h, m
                    if "." in rest:
                        second, micro = rest.split(".", 1)
                    else:
                        second = rest
                micro = f".{micro} microseconds" if micro else ""
                # keep leading space for parenthesis, don't strip it
                return f" (Date {year}:{month}:{day}, Time {hour}:{minute}, {second} seconds, {micro})"
            except Exception:
                return ""
        print(f"Found {len(sessions)} session(s) (chronological):")
        for tid, created, last, msg_count, step in sessions:
            print(f" - {tid}")
            print(f"   created: {created or 'unknown'}{_explain_ts(created) if created else ''}")
            print(f"   last message: {last or 'unknown'}{_explain_ts(last) if last else ''}")
            print(f"   (messages: {msg_count}, step: {step})")
    except Exception as e:
        print(f"Error listing sessions: {e}")
        import traceback
        traceback.print_exc()

async def inspect_session_store(thread_id: str):
    print(f"Inspecting session state for thread_id: '{thread_id}'...")
    config = {"configurable": {"thread_id": thread_id}}
    # Use alist for full history (all checkpoints), not just latest
    # alist = async iterator over CheckpointTuple history for thread_id
    # aget_tuple = single latest checkpoint
    try:
        # Use alist to show full message chain across all steps
        checkpoints = []
        async for tup in runtime.checkpointer.alist(config):
            checkpoints.append(tup)
        if not checkpoints:
            print("No state found for this thread.")
            return
        # alist returns newest first, reverse to show chronological
        checkpoints = list(reversed(checkpoints))
        print(f"Found {len(checkpoints)} checkpoint(s) for this thread.")
        # Show latest checkpoint's full state
        latest = checkpoints[-1]
        checkpoint = latest.checkpoint
        values = checkpoint.get("channel_values", {}) if isinstance(checkpoint, dict) else {}
        print(f"Available state keys: {list(values.keys())}")
        if latest.metadata:
            print(f"Metadata: {latest.metadata}")
        # files check: files in checkpoint is StateBackend scratchpad (/scratchpad/draft.md)
        # User can write via write_file("/scratchpad/...") tool, LLM can too.
        # StoreBackend (/context/...) is in virtual filesystem (inspect), not here.
        if 'files' in values:
            session_files = values['files']
            if isinstance(session_files, dict) and session_files:
                print(f"\nVirtual filesystem (scratchpad) files in session ({len(session_files)}):")
                for file_path, file_content in session_files.items():
                    print(f"\nPath: {file_path}")
                    content = file_content.get("content", "") if isinstance(file_content, dict) else str(file_content)
                    size = len(content)
                    print(f"  Size: {size} chars")
                    print(f"  Content:\n{content}")
            elif isinstance(session_files, dict):
                print("\nNo scratchpad files in this session (virtual filesystem files are in Store via `inspect`).")
            else:
                print("Session files format is unexpected.")
        else:
            print("\nNo scratchpad files in state (virtual filesystem files are in Store via `inspect`).")

        # Show ALL messages in full (the entire purpose of this function)
        if 'messages' in values:
            msgs = values['messages']
            print(f"\n=== Full session messages ({len(msgs)}) ===")
            for idx, m in enumerate(msgs, 1):
                # Handle both LangChain message objects and dicts
                if isinstance(m, dict):
                    mtype = m.get('type', 'unknown')
                    content = m.get('content', '')
                    tool_calls = m.get('tool_calls', [])
                    tool_call_id = m.get('tool_call_id')
                    name = m.get('name')
                else:
                    mtype = getattr(m, 'type', type(m).__name__)
                    content = getattr(m, 'content', '')
                    tool_calls = getattr(m, 'tool_calls', None)
                    tool_call_id = getattr(m, 'tool_call_id', None)
                    name = getattr(m, 'name', None)
                # Format content
                if isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict):
                            if c.get('type') == 'tool_call':
                                parts.append(f"[tool_call id={c.get('id')} name={c.get('name')} args={c.get('args')}]")
                            elif 'text' in c:
                                parts.append(str(c.get('text')))
                            else:
                                parts.append(str(c))
                        else:
                            parts.append(str(c))
                    content = "\n".join(parts) if parts else ""
                elif content is None:
                    content = ""
                content = str(content)
                print(f"\n--- Message {idx} [{mtype}] ---")
                if name:
                    print(f"  name: {name}")
                if tool_call_id:
                    print(f"  tool_call_id: {tool_call_id}")
                # Print content in full (no truncation)
                if content.strip():
                    print(content)
                else:
                    print("(no content)")
                # Show tool call details if present
                if tool_calls:
                    for tc in tool_calls:
                        if isinstance(tc, dict):
                            print(f"  [tool_call] {tc.get('name')} id={tc.get('id')} args={tc.get('args')}")
                        else:
                            # LangChain ToolCall object
                            print(f"  [tool_call] {getattr(tc, 'name', tc)} id={getattr(tc, 'id', '')} args={getattr(tc, 'args', '')}")
        # Also show history length via alist
        if len(checkpoints) > 1:
            print(f"\n--- History: {len(checkpoints)} checkpoints, latest step {latest.metadata.get('step', '?') if latest.metadata else '?'} ---")
    except Exception as e:
        print(f"Error fetching state: {e}")
        import traceback
        traceback.print_exc()
        return

async def export_file(store_path: str, local_path: Path):
    print(f"Exporting '{store_path}' to '{local_path}'...")
    item = await runtime.store.aget(("filesystem",), store_path)
    if not item:
        print(f"Error: File not found at '{store_path}' in the store.")
        return
    content = item.value.get("content", "")
    if isinstance(content, list):
        content = "\n".join(content)
    local_path.parent.mkdir(parents=True, exist_ok=True)
    local_path.write_text(content, encoding="utf-8")
    print(f"Successfully exported to {local_path.absolute()} ({len(content)} chars)\n")

async def delete_file(store_path: str):
    print(f"Deleting '{store_path}' from virtual filesystem...")
    namespace = ("filesystem",)
    try:
        await runtime.store.adelete(namespace, store_path)
        print(f"Successfully deleted '{store_path}'.\n")
    except Exception as e:
        print(f"Error deleting file: {e}\n")

async def main():
    examples = """
Examples:
  # Wipe the entire virtual filesystem
  python store_cli.py wipe --namespace all

  # Seed the virtual filesystem from a local directory
  python store_cli.py seed --dir ./context

  # Inspect the virtual filesystem (simple list)
  python store_cli.py inspect

  # Inspect the virtual filesystem with metadata and preview
  python store_cli.py inspect --show-size --show-dates --preview 50

  # List all sessions in checkpointer
  python store_cli.py list-sessions

  # Export a file from the virtual filesystem to local disk
  python store_cli.py export --store-path /findings/bloodhound_dump.txt --local-path ./exported_test.md

  # Delete a specific file from the virtual filesystem
  python store_cli.py delete --store-path /findings/bloodhound_dump.txt

  # inspect session history
  python store_cli.py inspect-session --thread-id 737280d4-4a28-4427-b851-de581174c3eb
"""

    parser = argparse.ArgumentParser(
        description="LangChain Store Debugging CLI (reuses AgentRuntime store/checkpointer, same agent_session.db)",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    p_seed = subparsers.add_parser("seed", help="Seed the virtual filesystem from a local directory")
    p_seed.add_argument("--dir", type=Path, default=Path("./context"), help="Local directory to seed from")

    p_wipe = subparsers.add_parser("wipe", help="Wipe the virtual filesystem")
    p_wipe.add_argument("--namespace", type=str, default="filesystem", help="Namespace to wipe (e.g., 'filesystem'). Use 'all' for root.")

    p_inspect = subparsers.add_parser("inspect", help="Inspect the virtual filesystem contents")
    p_inspect.add_argument("--namespace", type=str, default="filesystem", help="Namespace to inspect")
    p_inspect.add_argument("--show-size", action="store_true", help="Show file sizes")
    p_inspect.add_argument("--show-dates", action="store_true", help="Show creation/modification dates")
    p_inspect.add_argument("--preview", type=int, default=0, help="Number of characters for file preview (0 to disable)")

    p_list = subparsers.add_parser("list-sessions", help="List all thread_ids in checkpointer")

    p_export = subparsers.add_parser("export", help="Export a file from the virtual filesystem to local disk")
    p_export.add_argument("--store-path", type=str, required=True, help="Path in the virtual filesystem (e.g., /findings/bloodhound_dump.txt)")
    p_export.add_argument("--local-path", type=Path, required=True, help="Local path to save to")

    p_delete = subparsers.add_parser("delete", help="Delete a specific file from the virtual filesystem")
    p_delete.add_argument("--store-path", type=str, required=True, help="Path in the virtual filesystem to delete")

    p_session = subparsers.add_parser("inspect-session", help="Inspect full session history from checkpointer")
    p_session.add_argument("--thread-id", type=str, required=True, help="The thread_id (context_id) of the session")

    args = parser.parse_args()

    # Ensure runtime store/checkpointer ready (no LLM needed for store/session inspect)
    await _ensure_runtime_store()

    if args.command == "seed":
        await seed_store(args.dir)
    elif args.command == "wipe":
        ns = "" if args.namespace.lower() == "all" else args.namespace
        await wipe_store(ns)
    elif args.command == "inspect":
        await inspect_store(args.namespace, args.show_size, args.show_dates, args.preview)
    elif args.command == "list-sessions":
        await list_sessions()
    elif args.command == "export":
        await export_file(args.store_path, args.local_path)
    elif args.command == "delete":
        await delete_file(args.store_path)
    elif args.command == "inspect-session":
        await inspect_session_store(args.thread_id)
    else:
        parser.print_help()

    # Close async connections to allow loop to exit (aiosqlite keeps background thread)
    try:
        if runtime.checkpointer is not None and hasattr(runtime.checkpointer, 'conn'):
            await runtime.checkpointer.conn.close()
    except Exception:
        pass
    try:
        if runtime.store is not None and hasattr(runtime.store, 'conn'):
            await runtime.store.conn.close()
    except Exception:
        pass

if __name__ == "__main__":
    asyncio.run(main())
