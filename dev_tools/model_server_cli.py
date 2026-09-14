"""
Model Server CLI (dev tool) — v1.0
==================================

What this file is
-----------------
A CLI for interacting with the personal **model manager server**: a FastAPI
convenience server that launches ``llama.cpp`` models on a remote machine and
exposes them through an OpenAI-compatible API.  Version: 1.0.

What purpose does this file have
--------------------------------
It lets you control the remote model server and check that a model works
without writing any code:
  - launch a model (starts ``llama-server`` on the remote machine),
  - stop a running model,
  - check what is running / available and how long until idle timeout,
  - print the OpenAI-compatible endpoint + secret so it can be plugged
    straight into ``ChatOpenAI`` / the report agent,
  - smoke-test the ``/v1`` chat proxy with a single message.

What this file has
------------------
Commands (subcommands of the same script):
  * ``status``    — GET /models: running models, available models, idle countdown.
  * ``launch``    — POST /launch: start llama-server for a model, returns
                    port + LAN IP + secret (saved to *.model_server_state.json*).
  * ``stop``      — POST /stop: stop llama-server (uses saved secret by default).
  * ``endpoint``  — print the OpenAI-compatible base URL + API key + model
                    (``--env`` also prints them as env-var assignments).
  * ``chat``      — POST /v1/chat/completions smoke test through the proxy.

State
-----
Launch secrets are persisted to ``.model_server_state.json`` next to this file
(gitignored).  ``stop`` / ``endpoint`` / ``chat`` re-read it automatically.

How to use it
-------------
Run from the ``dev_tools`` directory (or with the full path):

  python model_server_cli.py status                          # what's up
  python model_server_cli.py launch --model ornith --gpu-layers 99
  python model_server_cli.py endpoint --env                  # env vars for the agent
  python model_server_cli.py chat --model ornith --message "hello" --max-tokens 2048
  python model_server_cli.py stop --model ornith

Default server: http://100.89.7.40:8050 (tailscale).  Override per call with
``--server http://<host>:<port>``.

NOTE (max tokens): ``chat`` replies are capped by ``--max-tokens`` (default
256) — long answers can look "cut off" mid-sentence.  Raise it (e.g.
``--max-tokens 2048``) for full replies.

Server (tailscale): http://100.89.7.40:8050  (FastAPI convenience server)
  GET  /models                -> live status, available models, idle countdown
  POST /launch                -> start llama-server, returns port + LAN IP + secret
  POST /stop                  -> stop llama-server (needs secret)
  POST/GET /v1/*              -> OpenAI-compatible proxy (Bearer <secret>)
"""

import argparse
import json
import sys
from pathlib import Path

import requests

if sys.platform == "win32":
    # Windows console defaults to cp1252 which crashes on emoji/unicode → force UTF-8
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_SERVER = "http://100.89.7.40:8050"
STATE_FILE = Path(__file__).parent / ".model_server_state.json"


def _server_url(args):
    return (args.server or DEFAULT_SERVER).rstrip("/")


def _load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def cmd_status(args):
    r = requests.get(f"{_server_url(args)}/models", timeout=args.timeout)
    r.raise_for_status()
    data = r.json()
    running = data.get("running", {})
    available = data.get("available", {})
    if not running:
        print("No models running.")
    else:
        print("Running:")
        for name, info in running.items():
            idle = info.get("idle_seconds_left", "?")
            print(f"  {name:12s} port={info.get('port')} gpu={info.get('gpu_layers')} "
                  f"idle_left={idle}s status={info.get('status')}")
    if not available:
        print("No available models registered.")
    else:
        print("Available:")
        for name, info in available.items():
            print(f"  {name:12s} {info.get('description', '')}")
    if args.json:
        print(json.dumps(data, indent=2))


def cmd_launch(args):
    payload = {"model": args.model, "gpu_layers": args.gpu_layers}
    r = requests.post(f"{_server_url(args)}/launch", json=payload, timeout=args.timeout)
    r.raise_for_status()
    data = r.json()
    state = _load_state()
    secret = (args.secret or data.get("secret") or data.get("secret_key") or "").strip()
    port = data.get("port") or (data.get("data") or {}).get("port")
    endpoint = args.endpoint or f"{_server_url(args)}/v1"
    state[args.model] = {
        "model": args.model,
        "port": port,
        "gpu_layers": args.gpu_layers,
        "secret": secret,
        "endpoint": endpoint,
        "response": data,
    }
    _save_state(state)
    print(f"Launched '{args.model}':")
    print(json.dumps(data, indent=2))
    if port and not args.endpoint:
        print(f"\nDirect llama-server endpoint (if reachable): http://{_server_url(args).split('//')[1].split(':')[0]}:{port}/v1")
    if secret:
        print(f"\nSaved secret + endpoint to {STATE_FILE}")


def cmd_stop(args):
    state = _load_state()
    secret = args.secret or (state.get(args.model) or {}).get("secret") or ""
    if not secret:
        print(f"ERROR: no secret for model '{args.model}'. Pass --secret or run launch again.")
        sys.exit(1)
    payload = {"model": args.model, "secret": secret}
    r = requests.post(f"{_server_url(args)}/stop", json=payload, timeout=args.timeout)
    r.raise_for_status()
    print(f"Stop requested for '{args.model}':")
    print(json.dumps(r.json(), indent=2))


def cmd_endpoint(args):
    state = _load_state()
    model = args.model or next(iter(state), None)
    if not model:
        print("No saved model. Run launch first (or pass --model).")
        sys.exit(1)
    info = state.get(model, {})
    secret = args.secret or info.get("secret") or ""
    base_url = args.endpoint or info.get("endpoint") or f"{_server_url(args)}/v1"
    print(f"Base URL: {base_url}")
    print(f"API Key : {secret}")
    print(f"Model   : {model}")
    if args.env:
        print(f"\n# PowerShell / Windows set-env (for report agent tests):")
        print(f'$env:LLM_BASE_URL = "{base_url}"')
        print(f'$env:LLM_API_KEY = "{secret}"')
        print(f'$env:LLM_MODEL = "{model}"')


def cmd_chat(args):
    """Quick OpenAI-compatible smoke test through the model manager /v1 proxy."""
    state = _load_state()
    model = args.model or next(iter(state), None)
    if not model:
        print("No saved model. Run launch first (or pass --model).")
        sys.exit(1)
    info = state.get(model, {})
    secret = args.secret or info.get("secret") or ""
    base_url = args.endpoint or info.get("endpoint") or f"{_server_url(args)}/v1"
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Authorization": f"Bearer {secret}"} if secret else {}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": args.message}],
        "max_tokens": args.max_tokens,
        "temperature": args.temperature,
    }
    print(f"POST {url}  model={model}")
    r = requests.post(url, json=payload, headers=headers, timeout=args.timeout)
    if r.status_code != 200:
        print(f"HTTP {r.status_code}: {r.text[:1000]}")
        sys.exit(1)
    data = r.json()
    content = data.get("choices", [{}])[0].get("message", {}).get("content")
    print("Reply:")
    print(content)


def main():
    epilog = """Examples:
  # Check what's running / available on the remote model machine
  python model_server_cli.py status

  # Launch the ornith model (0 gpu layers = CPU)
  python model_server_cli.py launch --model ornith --gpu-layers 0

  # Show the OpenAI-compatible endpoint + secret (env-form for report agent)
  python model_server_cli.py endpoint --env

  # Smoke-test the /v1 proxy (default --max-tokens 256 may cut long replies)
  python model_server_cli.py chat --model ornith --message "hi, say hello in 3 words"
  python model_server_cli.py chat --model ornith --message "what can you do?" --max-tokens 2048

  # Stop it (reads saved secret from state file)
  python model_server_cli.py stop --model ornith

  # Point at any server instead of the default
  python model_server_cli.py status --server http://10.8.1.4:8050
"""
    parser = argparse.ArgumentParser(
        description="Personal llama.cpp model manager server CLI",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--server", default=None, help=f"Model manager base URL (default {DEFAULT_SERVER})")
    parser.add_argument("--timeout", type=int, default=120, help="HTTP timeout seconds")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("status", help="GET /models: running + available models, idle countdown")
    p.add_argument("--json", action="store_true", help="Dump raw JSON")

    p = sub.add_parser("launch", help="POST /launch: start llama-server for a model")
    p.add_argument("--model", required=True)
    p.add_argument("--gpu-layers", type=int, default=0)
    p.add_argument("--secret", default=None, help="Secret returned by server (auto-saved otherwise)")
    p.add_argument("--endpoint", default=None, help="Override OpenAI base URL saved to state")

    p = sub.add_parser("stop", help="POST /stop: stop llama-server (uses saved secret unless --secret)")
    p.add_argument("--model", required=True)
    p.add_argument("--secret", default=None)

    p = sub.add_parser("endpoint", help="Print OpenAI-compatible endpoint + secret for ChatOpenAI")
    p.add_argument("--model", default=None, help="Model key from saved state (default: first saved)")
    p.add_argument("--secret", default=None)
    p.add_argument("--endpoint", default=None, help="Override base URL")
    p.add_argument("--env", action="store_true", help="Print as env-var settings for report agent")

    p = sub.add_parser("chat", help="Smoke-test the OpenAI-compatible /v1 proxy")
    p.add_argument("--model", default=None, help="Model key from saved state (default: first saved)")
    p.add_argument("--secret", default=None)
    p.add_argument("--endpoint", default=None, help="Override base URL (eg direct llama-server)")
    p.add_argument("--message", default="Reply with exactly: pong")
    p.add_argument("--max-tokens", type=int, default=256,
                   help="Max reply tokens (default 256; raise e.g. --max-tokens 2048 for full long answers)")
    p.add_argument("--temperature", type=float, default=0.2)

    args = parser.parse_args()
    {"status": cmd_status, "launch": cmd_launch, "stop": cmd_stop,
     "endpoint": cmd_endpoint, "chat": cmd_chat}[args.command](args)


if __name__ == "__main__":
    main()