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
    # Windows console defaults to cp1252 which crashes on emoji/unicode → force UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# Adjust path to import the store and agent
sys.path.append(str(Path(__file__).parent.parent))
from agent.report_agent import runtime

# No pool/store created at import - reused from runtime (same file as AgentRuntime LOCAL_DB_PATH)
# via await runtime._init_pool() in main(). No extra pool.

async def _ensure_runtime_store(force_sqlite: bool = False):
    if runtime.store is None or runtime.checkpointer is None:
        await runtime._init_pool(force_sqlite=force_sqlite)

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

async def _latest_thread_ids() -> list[str]:
    """Return ALL thread_ids, newest first, via one SQL request.

    ``checkpoint_id`` embeds a sortable timestamp, so MAX() = most recent
    activity. The caller slices/trims to what it displays and uses len() for
    the real total - the CLI only ever ``alist``-scans the handful of sessions
    it prints, never the whole table.
    """
    # Postgres: psycopg async pool, dict_row row factory.
    if runtime.pool is not None:
        async with runtime.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT thread_id, MAX(checkpoint_id) AS last_id "
                    "FROM checkpoints GROUP BY thread_id "
                    "ORDER BY last_id DESC"
                )
                rows = await cur.fetchall()
        return [str(row["thread_id"]) for row in rows]

    # SQLite fallback: the checkpointer's own aiosqlite connection points at
    # the ACTIVE local file (LOCAL_DB_PATH), so no hardcoded path is needed.
    if runtime.checkpointer is not None and hasattr(runtime.checkpointer, "conn"):
        async with runtime.checkpointer.conn.execute(
            "SELECT thread_id, MAX(checkpoint_id) AS last_id "
            "FROM checkpoints GROUP BY thread_id "
            "ORDER BY last_id DESC"
        ) as cur:
            rows = await cur.fetchall()
        return [str(row[0]) for row in rows]

    return []

async def list_sessions():
    """List all thread_ids chronologically with created/last timestamps."""
    print("Listing sessions in checkpointer chronologically...", flush=True)
    try:
        # ONE SQL request returns ALL thread_ids newest-first; total = len().
        # Only then do we slice the newest N and alist-scan just those, so the
        # CLI knows the real total without ever scanning every thread.
        tids = await _latest_thread_ids()
        total = len(tids)
        if not tids:
            print("No sessions found.")
            return
        sessions = []
        for tid in tids[:10]:
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
        # Sort by created ts: newest FIRST (ISO strings sort correctly, reversed)
        sessions.sort(key=lambda x: x[1] or "", reverse=True)
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
        MAX_SHOWN = 10
        if total > MAX_SHOWN:
            print(f"Found {total} session(s) total - this tool currently lists only the "
                  f"LATEST {MAX_SHOWN} (newest first):")
        else:
            print(f"Found {total} session(s) (newest first):")
        for tid, created, last, msg_count, step in sessions[:MAX_SHOWN]:
            print(f" - {tid}")
            print(f"   created: {created or 'unknown'}{_explain_ts(created) if created else ''}")
            print(f"   last message: {last or 'unknown'}{_explain_ts(last) if last else ''}")
            print(f"   (messages: {msg_count}, step: {step})")
    except Exception as e:
        print(f"Error listing sessions: {e}")
        import traceback
        traceback.print_exc()

async def inspect_session_store(thread_id: str, last_n: int = 0, full_reasoning: bool = False):
    if last_n and last_n > 0:
        print(f"(showing last {last_n} message(s) — full history stays in the checkpointer)")
    print(f"Inspecting session state for thread_id: '{thread_id}'...")
    # The agent's own checkpoints live under the empty namespace; the UI chat
    # copy lives under checkpoint_ns="ui" (metadata {"source": "ui"}). Without
    # checkpoint_ns, alist() returns both, and the reversed() + [-1] "latest"
    # here would land on the UI display checkpoint instead of the real agent
    # state. Scope to the agent namespace so the count and the shown messages
    # (incl. tool calls + results) are the true conversation history.
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
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
        print(f"Found {len(checkpoints)} agent checkpoint(s) for this thread.")
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
            total = len(msgs)
            if last_n and last_n > 0 and total > last_n:
                msgs = msgs[-last_n:]
            print(f"\n=== Full session messages ({len(msgs)} of {total}) ===")
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
                # Model reasoning, where the backend surfaced any. The local
                # backend needs ReasoningChatOpenAI to keep the field at all
                # (#18); OpenRouter's client extracts it itself. Collapsed by
                # default because it is routinely longer than the answer.
                reasoning = ''
                if isinstance(m, dict):
                    reasoning = (m.get('additional_kwargs') or {}).get('reasoning_content') or ''
                else:
                    reasoning = (getattr(m, 'additional_kwargs', None) or {}).get('reasoning_content') or ''
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
                if isinstance(reasoning, str) and reasoning.strip():
                    lines = reasoning.strip().splitlines()
                    if full_reasoning:
                        print(f"  reasoning ({len(reasoning)} chars, {len(lines)} lines):")
                        for rl in lines:
                            print(f"    {rl}")
                    else:
                        print(f"  reasoning ({len(reasoning)} chars, {len(lines)} lines): {lines[0]}")
                        if len(lines) > 1:
                            print("    ... use --full-reasoning to print it all")
                if tool_call_id:
                    print(f"  tool_call_id: {tool_call_id}")
                # Flat UI format carries args/result directly on the dict
                if isinstance(m, dict):
                    if m.get("args"):
                        print(f"  args: {m.get('args')}")
                    if m.get("result"):
                        result_preview = str(m.get("result"))
                        print(f"  result: {result_preview[:400]}{'...' if len(result_preview) > 400 else ''}")
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
    namespace = ("filesystem",)
    item = await runtime.store.aget(namespace, store_path)
    if not item and not store_path.startswith("/"):
        item = await runtime.store.aget(namespace, "/" + store_path)
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
        deleted = await runtime.store.adelete(namespace, store_path)
        if not deleted and not store_path.startswith("/"):
            deleted = await runtime.store.adelete(namespace, "/" + store_path)
        print(f"Successfully deleted '{store_path}'.\n" if deleted else f"Not found: '{store_path}'.\n")
    except Exception as e:
        print(f"Error deleting file: {e}\n")

async def reconcile_files():
    """Normalize filesystem keys to the agent-canonical slash form.

    The agent's ``StoreBackend`` keys ``/context/...`` files with a leading
    slash (``/findings/x``), while older UI uploads stored them without one
    (``findings/x``).  This renames any no-slash rows to the slash form so the
    agent (and the UI, which reads slash-first) address the same entries.
    """
    print("Reconciling virtual filesystem keys (no-slash -> /slash)...")
    namespace = ("filesystem",)
    items = await runtime.store.asearch(namespace, limit=10000)
    changed = 0
    for item in items:
        key = item.key
        if key.startswith("/"):
            continue
        new_key = "/" + key
        existing = await runtime.store.aget(namespace, new_key)
        if existing is None:
            await runtime.store.aput(namespace, new_key, item.value)
            print(f"  {key!r} -> {new_key!r}")
        else:
            print(f"  {key!r}: target {new_key!r} already exists - dropping duplicate")
        await runtime.store.adelete(namespace, key)
        changed += 1
    print(f"Done: {changed} file(s) normalized. The agent can now see them under /findings | /reports | ...\n")

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

  # Normalize legacy no-slash filesystem keys to the agent's /slash form
  python store_cli.py reconcile-files
"""

    parser = argparse.ArgumentParser(
        description="LangChain Store Debugging CLI (reuses AgentRuntime store/checkpointer, same agent_session.db)",
        epilog=examples,
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--sqlite", action="store_true",
                        help="Force local SQLite backend (skip Postgres), even when POSTGRES_URI is set/reachable.")
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
    p_session.add_argument("--tail", type=int, default=0, help="Show only the last N messages (0 = all)")
    p_session.add_argument("--full-reasoning", action="store_true", help="Print each message's full reasoning instead of a one-line summary");
    p_session.add_argument("--out", type=Path, default=None, help="Also write full output to this file (for long threads that overflow the cmd window)")

    p_reconcile = subparsers.add_parser("reconcile-files", help="Normalize filesystem keys to the agent's /slash form")

    args = parser.parse_args()

    # Ensure runtime store/checkpointer ready (no LLM needed for store/session inspect)
    await _ensure_runtime_store(force_sqlite=args.sqlite)

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
        if args.out:
            # Tee: full output goes to file AND console (cmd window cuts long
            # threads, the file keeps everything).
            import io
            buf = io.StringIO()
            import contextlib
            with contextlib.redirect_stdout(buf):
                await inspect_session_store(args.thread_id, last_n=args.tail, full_reasoning=args.full_reasoning)
            text = buf.getvalue()
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(text, encoding="utf-8")
            print(text)
            print(f"(full output also saved to {args.out.absolute()})")
        else:
            await inspect_session_store(args.thread_id, last_n=args.tail, full_reasoning=args.full_reasoning)
    elif args.command == "reconcile-files":
        await reconcile_files()
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
