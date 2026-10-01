"""
A2A Client CLI (dev tool)
=========================

Thin command-line wrapper around the unified ``client.A2AClient`` — every
public client method is one subcommand, so the live server's task store can be
inspected and driven without writing any code:

  * ``list``      — ListTasks (ids, states, contexts, paging)
  * ``send``      — send_message (one-shot) or send_streaming_message (default)
  * ``get``       — GetTask (state, history, artifacts)
  * ``cancel``    — CancelTask
  * ``subscribe`` — SubscribeToTask (streams remaining updates; fails on
                    terminal tasks — use ``get`` for those)
  * ``poll``      — poll get_task until COMPLETED (helper, not a protocol op)

State is file-backed nowhere: the server keeps an InMemoryTaskStore, so tasks
vanish on server restart while Postgres sessions persist (see store_cli.py).

How to use it (run from the repo root ``report agent/``):
  python dev_tools/a2a_cli.py list
  python dev_tools/a2a_cli.py list --status WORKING --page-size 20
  python dev_tools/a2a_cli.py send "Generate a recon summary" --no-stream
  python dev_tools/a2a_cli.py get --task <task_id>
  python dev_tools/a2a_cli.py cancel --task <task_id>
  python dev_tools/a2a_cli.py subscribe --task <task_id>
  python dev_tools/a2a_cli.py poll --task <task_id> --interval 5 --attempts 30

Server address: ``--server`` flag, else ``A2A_SERVER_URL`` env,
else ``http://127.0.0.1:9999``.
"""

import argparse
import asyncio
import os
import sys

import httpx

if sys.platform == "win32":
    # Windows console defaults to cp1252 which crashes on unicode in agent
    # output (arrows etc.) — force UTF-8 like the other dev tools do.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from client.a2a_client import A2AClient
from a2a.types.a2a_pb2 import TaskState
from a2a.helpers import get_message_text

DEFAULT_SERVER = "http://127.0.0.1:9999"

STATE_NAMES = {
    int(TaskState.TASK_STATE_SUBMITTED): "SUBMITTED",
    int(TaskState.TASK_STATE_WORKING): "WORKING",
    int(TaskState.TASK_STATE_COMPLETED): "COMPLETED",
    int(TaskState.TASK_STATE_CANCELED): "CANCELED",
    int(TaskState.TASK_STATE_FAILED): "FAILED",
}


def _server_url(args):
    return (args.server or os.getenv("A2A_SERVER_URL") or DEFAULT_SERVER).rstrip("/")


def _state_name(state):
    return STATE_NAMES.get(int(state), str(state))


def _parse_status(value):
    """Accept '3' or 'WORKING' (case-insensitive)."""
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        pass
    key = f"TASK_STATE_{str(value).upper()}"
    if hasattr(TaskState, key):
        return int(getattr(TaskState, key))
    raise ValueError(f"unknown status {value!r} (use int or SUBMITTED/WORKING/COMPLETED/CANCELED/FAILED)")


def _print_task_summary(task):
    print(f"  id:         {task.id}")
    print(f"  state:      {_state_name(task.status.state)}")
    print(f"  context_id: {task.context_id}")
    print(f"  history:    {len(task.history)} message(s)")
    print(f"  artifacts:  {len(task.artifacts)}")
    status_text = get_message_text(task.status.message) if task.status.message else None
    if status_text:
        print(f"  status:     {status_text[:200]}")


def _print_task_full(task):
    _print_task_summary(task)
    if task.history:
        print("  --- history (last 3) ---")
        for h in task.history[-3:]:
            print(f"    {(get_message_text(h) or '')[:200]}")
    for art in task.artifacts:
        for part in art.parts:
            text = getattr(part, "text", "") or ""
            if text:
                print(f"  --- artifact ({len(text)} chars) ---")
                print(f"    {text[:500]}")


async def _make_client(server_url):
    httpx_client = httpx.AsyncClient(timeout=300.0)
    client = await A2AClient.create(server_url, streaming=True, httpx_client=httpx_client)
    return client, httpx_client


async def cmd_list(args):
    client, httpx_client = await _make_client(_server_url(args))
    try:
        # The store returns oldest-first per page, so collect all pages and
        # print newest-first (task store carries no timestamps to sort by).
        tasks = []
        page_token = None
        while True:
            resp = await client.list_tasks(
                context_id=args.context_id,
                status=_parse_status(args.status),
                page_size=args.page_size,
                page_token=page_token,
            )
            tasks.extend(resp.tasks)
            page_token = resp.next_page_token or None
            if not page_token:
                break
        print(f"{len(tasks)} task(s), newest first")
        for task in reversed(tasks):
            print(f"  {task.id}  {_state_name(task.status.state):10s}  ctx={task.context_id}")
    finally:
        await client.close()
        await httpx_client.aclose()


async def cmd_send(args):
    client, httpx_client = await _make_client(_server_url(args))
    try:
        if args.no_stream:
            task_id, ctx, text = await client.send_message(args.text, context_id=args.context_id)
            print(f"task_id:    {task_id}")
            print(f"context_id: {ctx}")
            print(f"first:      {(text or '')[:500]}")
        else:
            task_id, ctx, artifact = await client.send_streaming_message(
                args.text, context_id=args.context_id
            )
            print(f"task_id:    {task_id}")
            print(f"context_id: {ctx}")
            print(f"artifact:   {(artifact or '')[:500]}")
    finally:
        await client.close()
        await httpx_client.aclose()


async def cmd_get(args):
    client, httpx_client = await _make_client(_server_url(args))
    try:
        _print_task_full(await client.get_task(args.task))
    finally:
        await client.close()
        await httpx_client.aclose()


async def cmd_cancel(args):
    client, httpx_client = await _make_client(_server_url(args))
    try:
        _print_task_summary(await client.cancel_task(args.task))
    finally:
        await client.close()
        await httpx_client.aclose()


async def cmd_subscribe(args):
    client, httpx_client = await _make_client(_server_url(args))
    try:
        task_id, ctx, artifact = await client.subscribe_to_task(args.task)
        print(f"task_id:    {task_id}")
        print(f"context_id: {ctx}")
        print(f"artifact:   {(artifact or '')[:500]}")
    finally:
        await client.close()
        await httpx_client.aclose()


async def cmd_poll(args):
    client, httpx_client = await _make_client(_server_url(args))
    try:
        task = await client.poll_until_complete(
            args.task, interval=args.interval, max_attempts=args.attempts
        )
        _print_task_summary(task)
    finally:
        await client.close()
        await httpx_client.aclose()


def main():
    parser = argparse.ArgumentParser(description="A2A client CLI (wraps client.A2AClient).")
    parser.add_argument("--server", default=None, help="A2A server base URL")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="ListTasks")
    p_list.add_argument("--context-id", default=None)
    p_list.add_argument("--status", default=None, help="int or SUBMITTED/WORKING/...")
    p_list.add_argument("--page-size", type=int, default=20)
    p_list.set_defaults(func=cmd_list)

    p_send = sub.add_parser("send", help="send_message / send_streaming_message")
    p_send.add_argument("text", help="message text to send")
    p_send.add_argument("--context-id", default=None, help="reuse for multi-turn")
    p_send.add_argument("--no-stream", action="store_true", help="single Task reply")
    p_send.set_defaults(func=cmd_send)

    p_get = sub.add_parser("get", help="GetTask")
    p_get.add_argument("--task", required=True)
    p_get.set_defaults(func=cmd_get)

    p_cancel = sub.add_parser("cancel", help="CancelTask")
    p_cancel.add_argument("--task", required=True)
    p_cancel.set_defaults(func=cmd_cancel)

    p_sub = sub.add_parser("subscribe", help="SubscribeToTask (live tasks only)")
    p_sub.add_argument("--task", required=True)
    p_sub.set_defaults(func=cmd_subscribe)

    p_poll = sub.add_parser("poll", help="poll get_task until COMPLETED")
    p_poll.add_argument("--task", required=True)
    p_poll.add_argument("--interval", type=float, default=5.0)
    p_poll.add_argument("--attempts", type=int, default=30)
    p_poll.set_defaults(func=cmd_poll)

    args = parser.parse_args()
    asyncio.run(args.func(args))


if __name__ == "__main__":
    main()
