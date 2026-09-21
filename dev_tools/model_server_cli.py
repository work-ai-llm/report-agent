"""
Model Server CLI (dev tool) — v1.3
==================================

What this file is
-----------------
A CLI for interacting with the personal **model manager server**: a FastAPI
convenience server that launches ``llama.cpp`` models on a remote machine and
exposes them through an OpenAI-compatible API.  Version: 1.3.

What purpose does this file have
--------------------------------
It lets you control the remote model server and answer "which model / how much
context / how fast" questions without writing any code:
  - launch a model (starts ``llama-server`` on the remote machine),
  - stop a running model,
  - check what is running / available and how long until idle timeout,
  - inspect live hardware (RAM / VRAM) and how much context fits per KV dtype,
  - read the precomputed speed estimates (CPU speeds, preset minutes, measured
    GPU samples) — no per-parameter compute is exposed; with ``--model`` the
    server also reports its own recommended GPU offload split,
  - print the OpenAI-compatible endpoint + secret so it can be plugged
    straight into ``ChatOpenAI`` / the report agent,
  - smoke-test the ``/v1`` chat proxy with a single message.

What this file has
------------------
Commands (subcommands of the same script):
  * ``status``    — GET /models: running + available models, idle countdown,
                    quant info, native context, KV cost, size.
  * ``launch``    — POST /launch: start llama-server for a model, returns
                    port + LAN IP + secret (saved to *.model_server_state.json*).
                    Supports ``--ctx`` and KV-cache dtype ``--ctk`` / ``--ctv``.
  * ``stop``      — POST /stop: stop llama-server (uses saved secret by default).
  * ``resources`` — GET /resources: live RAM / per-GPU VRAM (GB), and
                    ``fits_full_gpu`` / ``fits_ram`` max context in tokens per
                    model per KV dtype (with approx. GB of KV cache).
  * ``estimate``  — GET /estimate.  No args: the precomputed tables — speeds
                    (prompt/gen tps, CPU), presets (minutes for standard
                    workloads), and measured GPU ngl samples.  With
                    ``--model <name>`` it narrows to one model and additionally
                    shows the server-computed context budget + recommended GPU
                    offload.  The tool sends no per-parameter compute flags.
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

  python model_server_cli.py status
  python model_server_cli.py launch --model ornith --gpu-layers 33
  python model_server_cli.py launch --model ornith --gpu-layers 33 --ctk q8_0
  python model_server_cli.py resources
  python model_server_cli.py estimate
  python model_server_cli.py estimate --model ornith
  python model_server_cli.py endpoint --env
  python model_server_cli.py chat --model ornith --message "hello" --max-tokens 2048
  python model_server_cli.py stop --model ornith

Default server: http://100.89.7.40:8050 (tailscale).  Override per call with
``--server http://<host>:<port>`` (before or after the subcommand).

NOTE (max tokens): ``chat`` replies are capped by ``--max-tokens`` (default
256) — long answers can look "cut off" mid-sentence.  Raise it (e.g.
``--max-tokens 2048``) for full replies.

NOTE (KV-cache dtype): ``--ctk`` / ``--ctv`` on ``launch`` pick the KV cache
data type (``f16`` = default, ``q8_0``, ``q4_0``, ...); ``--ctx`` must be
within the model's native limit (server validates, returns 422 otherwise).
``--gpu-layers`` must be ≤ the model's layer count from ``status``
(e.g. ornith max 33).

Server (tailscale): http://100.89.7.40:8050  (FastAPI convenience server)
  GET  /models                -> status, metadata (quant, native_ctx, KV cost, ...)
  POST /launch                -> start llama-server (ctk/ctv/ctx validated)
  POST /stop                  -> stop llama-server (needs secret)
  GET  /resources             -> live RAM / VRAM, max context per model per KV dtype
  GET  /estimate              -> precomputed speeds/presets/samples (+ per-model rec)
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

VALID_KV_DTYPES = ("f16", "f32", "q8_0", "q4_0", "q4_1", "q5_0", "q5_1", "i8", "i16", "i32")


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


def _fmt_gb(b):
    if b is None:
        return "?"
    return f"{b / 2**30:.1f}"


def _fmt_num(n):
    if n is None or n == "?":
        return "?"
    return f"{n:,}"


def _show_running(running: dict):
    if not running:
        print("No models running.")
        return
    print("Running:")
    for name, info in running.items():
        idle = info.get("idle_seconds_left", "?")
        gpu = info.get("gpu_layers", "?")
        ctx = info.get("ctx", "?")
        ctk = info.get("cache_type_k", "?")
        ctv = info.get("cache_type_v", "?")
        status = info.get("status", "?")
        print(f"  {name:14s} port={info.get('port')} gpu={gpu} ctx={ctx}  "
              f"cache_k={ctk} cache_v={ctv}  idle_left={idle}s status={status}")
        note = info.get("ctx_note")
        if note:
            print(f"  ! ctx: {note}")
        s_note = info.get("streaming")
        if s_note:
            print(f"  ! streaming: {s_note}")


def _show_available(available: dict):
    if not available:
        print("No available models registered.")
        return
    print("Available:")
    for name, info in available.items():
        desc = info.get("description", "")
        quant = info.get("quant", "")
        pct = info.get("quant_pct", "")
        layers = info.get("layers", "")
        native_ctx = info.get("native_ctx", "")
        kv_bpt = info.get("kv_bytes_per_token", {})
        size_gb = info.get("size_gb", "")
        line = f"  {name:14s}"
        if quant:
            line += f"  {quant}"
        if pct:
            line += f" ({pct})"
        if layers:
            line += f"  layers={layers}"
        if native_ctx:
            line += f"  native_ctx={native_ctx}"
        if size_gb:
            line += f"  {size_gb}GB"
        if kv_bpt:
            line += f"  kv/tok(kB): {kv_bpt}"
        if desc:
            line += f"  {desc}"
        print(line)
    # show field_notes if present
    fn = next(iter(available.values()), {}).get("field_notes")
    if fn:
        print(f"  [field_notes] {fn}")


# ---- commands ----------------------------------------------------------------

def cmd_status(args):
    r = requests.get(f"{_server_url(args)}/models", timeout=args.timeout)
    r.raise_for_status()
    data = r.json()
    _show_running(data.get("running", {}))
    _show_available(data.get("available", {}))
    if args.json:
        print(json.dumps(data, indent=2))


def cmd_launch(args):
    if args.ctk and args.ctk not in VALID_KV_DTYPES:
        print(f"ERROR: --ctk must be one of {VALID_KV_DTYPES}")
        sys.exit(1)
    if args.ctv and args.ctv not in VALID_KV_DTYPES:
        print(f"ERROR: --ctv must be one of {VALID_KV_DTYPES}")
        sys.exit(1)
    payload = {"model": args.model, "gpu_layers": args.gpu_layers}
    if args.ctx is not None:
        payload["ctx"] = args.ctx
    if args.ctk:
        payload["ctk"] = args.ctk
    if args.ctv:
        payload["ctv"] = args.ctv
    r = requests.post(f"{_server_url(args)}/launch", json=payload, timeout=args.timeout)
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text)
        except Exception:
            detail = r.text
        print(f"ERROR: {detail}")
        sys.exit(1)
    data = r.json()
    state = _load_state()
    secret = (args.secret or data.get("secret") or data.get("secret_key") or "").strip()
    port = data.get("port") or (data.get("data") or {}).get("port")
    endpoint = args.endpoint or f"{_server_url(args)}/v1"
    state[args.model] = {
        "model": args.model,
        "port": port,
        "gpu_layers": args.gpu_layers,
        "ctk": args.ctk,
        "ctv": args.ctv,
        "ctx": args.ctx,
        "secret": secret,
        "endpoint": endpoint,
        "response": data,
    }
    _save_state(state)
    print(f"Launched '{args.model}':")
    print(json.dumps(data, indent=2))
    note = data.get("ctx_note")
    if note:
        print(f"\nWARNING: {note}")
    if port and not args.endpoint:
        print(f"\nDirect llama-server endpoint (if reachable): "
              f"http://{_server_url(args).split('//')[1].split(':')[0]}:{port}/v1")
    if secret:
        print(f"\nSaved secret + endpoint to {STATE_FILE}")
    s_note = data.get("streaming")
    if s_note:
        print(f"  ! streaming: {s_note}")


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


def _dtype_order(dtypes):
    preferred = ("f16", "q8_0", "q4_0")
    ranked = [d for d in preferred if d in dtypes]
    ranked += sorted(d for d in dtypes if d not in preferred)
    return ranked


def cmd_resources(args):
    r = requests.get(f"{_server_url(args)}/resources", timeout=args.timeout)
    r.raise_for_status()
    data = r.json()
    # kv_bytes_per_token from /models is KB of KV-cache per token per dtype
    kv_tok = {}
    try:
        mr = requests.get(f"{_server_url(args)}/models", timeout=args.timeout)
        if mr.ok:
            kv_tok = {name: (info.get("kv_bytes_per_token") or {})
                      for name, info in (mr.json().get("available") or {}).items()}
    except requests.RequestException:
        pass

    print("Live hardware:")
    ram = data.get("ram", {})
    if ram.get("total_bytes") is not None:
        print(f"  RAM: {_fmt_gb(ram.get('total_bytes'))} GB total, "
              f"{_fmt_gb(ram.get('free_bytes'))} GB free"
              + (f" (used {_fmt_gb(ram.get('used_bytes'))} GB, load {ram.get('load_percent')}%)"
                 if ram.get("used_bytes") is not None else ""))
    for i, gpu in enumerate(data.get("vram", [])):
        print(f"  GPU {gpu.get('device', i)}: {_fmt_gb(gpu.get('total_bytes'))} GB total, "
              f"{_fmt_gb(gpu.get('free_bytes'))} GB free")
    for section in ("fits_full_gpu", "fits_ram"):
        m = data.get(section, {})
        if not m:
            continue
        if section == "fits_full_gpu":
            label = "Max context in tokens (full GPU offload — fits free VRAM):"
        else:
            label = "Max context in tokens (CPU-only — fits free RAM):"
        print(f"\n{label}")
        dtypes = _dtype_order({d for v in m.values() for d in v})
        for name, kv in m.items():
            parts = "  "
            for d in dtypes:
                tok = kv.get(d, 0)
                cell = f"{d}={_fmt_num(tok)}"
                kb_per_tok = (kv_tok.get(name) or {}).get(d)
                if tok and kb_per_tok:
                    cell += f" (~{tok * kb_per_tok / 1024**2:.2f}GB KV)"
                parts += f"{cell:>26s}"
            print(f"  {name:14s}{parts}")
        fn = data.get("field_notes", {}).get(section)
        if fn:
            print(f"  # {fn}")
    if args.json:
        print(json.dumps(data, indent=2))


def cmd_estimate(args):
    params = {}
    if args.model:
        params["model"] = args.model
    r = requests.get(f"{_server_url(args)}/estimate", params=params, timeout=args.timeout)
    if r.status_code != 200:
        try:
            detail = r.json().get("detail", r.text[:500])
        except Exception:
            detail = r.text[:500]
        print(f"ERROR: {detail}")
        sys.exit(1)
    data = r.json()
    single = bool(data.get("model"))
    notes = data.get("field_notes", {})

    if data.get("mode"):
        print(f"Mode : {data['mode']}")
    if data.get("basis"):
        print(f"Basis: {data['basis']}")
    print()

    # 1) precomputed CPU speeds
    speeds = data.get("speeds")
    if speeds:
        print("Speeds (tokens/s, CPU-only, precomputed):")
        rows = {data.get("model"): speeds} if single else speeds
        for name, s in rows.items():
            print(f"  {name:14s} prompt={s.get('prompt_tps', '?')} tps   "
                  f"generation={s.get('generation_tps', '?')} tps")
        print()

    # 2) precomputed preset minutes
    presets = data.get("presets")
    if presets:
        cols = ("gen_1k", "pp_30k", "pp_60k", "turn_30k+1k", "turn_60k+1k")
        keys = ("gen_1000_min", "pp_30000_min", "pp_60000_min",
                "turn_30000_1000_min", "turn_60000_1000_min")
        print("Presets (minutes, CPU-only, precomputed):")
        rows = {data.get("model"): presets} if single else presets
        hdr = f"  {'model':14s}" + "".join(f"{c:>14s}" for c in cols)
        print(hdr)
        for name, p in rows.items():
            cells = "".join(f"{p.get(k, '?'):>14}" for k in keys)
            print(f"  {name:14s}{cells}")
        fn = notes.get("gen_1000_min")
        if fn:
            print(f"  # {fn}")
        print()

    # 3) precomputed measured GPU samples (per benchmarked ngl)
    samples = data.get("gpu_samples") or (data.get("partial_gpu") or {}).get("measured_points")
    if samples:
        print("Measured GPU samples (prompt_tps / generation_tps per offloaded layer, precomputed):")
        if single:
            print(f"    {'ngl':>5s} {'prompt_tps':>11s} {'gen_tps':>9s}")
            for ngl, s in sorted(samples.items(), key=lambda kv: int(kv[0])):
                print(f"    {ngl:>5s} {s.get('prompt_tps', '?'):>11} {s.get('generation_tps', '?'):>9}")
        else:
            for name, pts in samples.items():
                cells = []
                for n, s in sorted(pts.items(), key=lambda kv: int(kv[0])):
                    cells.append("ngl{0}={1}/{2}".format(n, s.get("prompt_tps", "?"),
                                                         s.get("generation_tps", "?")))
                print(f"  {name:14s}  {'  '.join(cells)}")
        print()

    # when queried with a model, the server (not the tool) computes the context
    # budget + a recommended offload split for the full native context
    pg = data.get("partial_gpu")
    if pg:
        cc = pg.get("ctx_current", {})
        if cc:
            print(f"Context budget (server, using live free mem): "
                  f"ram={_fmt_gb(cc.get('ram_free_bytes'))} GB  "
                  f"vram={_fmt_gb(cc.get('vram_free_bytes'))} GB  max_ngl={cc.get('ngl_blocks_limit')}")
            print()
        cm = pg.get("ctx_max_by_ngl", {})
        if cm:
            dtypes = _dtype_order({d for v in cm.values() for d in v})
            print("Max context per ngl / KV dtype (tokens):")
            hdr = f"    {'ngl':>5s}" + "".join(f"{d:>12s}" for d in dtypes)
            print(hdr)
            for ngl, v in sorted(cm.items(), key=lambda kv: int(kv[0])):
                cells = "".join(f"{_fmt_num(v.get(d, 0)):>12s}" for d in dtypes)
                print(f"    {ngl:>5s}{cells}")
            print()
        rec = pg.get("recommended")
        if rec:
            sp = rec.get("speeds", {})
            print("Recommended offload (server-computed, for full native context):")
            print(f"    ngl={rec.get('ngl')}  kv_dtype={rec.get('kv_dtype')}  "
                  f"target_ctx={_fmt_num(rec.get('target_ctx'))}  fits={rec.get('fits')}  "
                  f"ctx_max={_fmt_num(rec.get('ctx_max'))}")
            print(f"    speeds: prompt={sp.get('prompt_tps', '?')} tps / "
                  f"gen={sp.get('generation_tps', '?')} tps  interpolated={rec.get('interpolated')}")
            print()

    if args.json:
        print(json.dumps(data, indent=2))

# NOTE, this seem to be broken
# it seem to show old env state of the model launch (if you do another launch --env still shows old model and old api key)
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
  # Check what's running / available
  python model_server_cli.py status

  # Launch with full GPU offload (ornith max = 33; see `status`)
  python model_server_cli.py launch --model ornith --gpu-layers 33

  # Launch with CPU-only + q8 KV cache
  python model_server_cli.py launch --model ornith --gpu-layers 0 --ctk q8_0

  # Launch with custom context window
  python model_server_cli.py launch --model ornith --gpu-layers 33 --ctx 8192

  # See live RAM / VRAM and max context per model
  python model_server_cli.py resources

  # Precomputed estimate tables (speeds / presets / GPU samples) for all models
  python model_server_cli.py estimate

  # Same, for one model (adds the server's recommended offload split)
  python model_server_cli.py estimate --model ornith

  # Show env vars for the report agent
  python model_server_cli.py endpoint --env

  # Smoke-test chat (default --max-tokens 256 — raise for long replies)
  python model_server_cli.py chat --model ornith --message "hello" --max-tokens 2048

  # Stop (reads saved secret from state)
  python model_server_cli.py stop --model ornith

  # Point at a different server
  python model_server_cli.py status --server http://10.8.1.4:8050
"""
    parser = argparse.ArgumentParser(
        description="Personal llama.cpp model manager server CLI (v1.3)",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--server", default=None,
                        help=f"Model manager base URL (default {DEFAULT_SERVER})")
    parser.add_argument("--timeout", type=int, default=120,
                        help="HTTP timeout seconds")
    sub = parser.add_subparsers(dest="command", required=True)

    # every subcommand also accepts --server / --timeout, so they may be placed
    # before OR after the subcommand name
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--server", default=None,
                        help=f"Model manager base URL (default {DEFAULT_SERVER})")
    common.add_argument("--timeout", type=int, default=120,
                        help="HTTP timeout seconds")

    # status
    p = sub.add_parser("status",
                       help="GET /models: running + available models, idle countdown",
                       parents=[common])
    p.add_argument("--json", action="store_true", help="Dump raw JSON")

    # launch
    p = sub.add_parser("launch",
                       help="POST /launch: start llama-server for a model",
                       parents=[common])
    p.add_argument("--model", required=True, help="Registered model name")
    p.add_argument("--gpu-layers", type=int, default=0,
                   help="Number of layers to offload to GPU (0 = CPU-only)")
    p.add_argument("--ctx", type=int, default=None,
                   help="Context window size (must be 1..native_ctx; server validates)")
    p.add_argument("--ctk", default=None, metavar="DTYPE",
                   help=f"KV-cache K dtype ({', '.join(VALID_KV_DTYPES)}; default f16)")
    p.add_argument("--ctv", default=None, metavar="DTYPE",
                   help=f"KV-cache V dtype ({', '.join(VALID_KV_DTYPES)}; default f16)")
    p.add_argument("--secret", default=None,
                   help="Secret returned by server (auto-saved otherwise)")
    p.add_argument("--endpoint", default=None,
                   help="Override OpenAI base URL saved to state")

    # stop
    p = sub.add_parser("stop",
                       help="POST /stop: stop llama-server (uses saved secret unless --secret)",
                       parents=[common])
    p.add_argument("--model", required=True)
    p.add_argument("--secret", default=None)

    # resources
    p = sub.add_parser("resources",
                       help="GET /resources: live RAM / VRAM, max context per model per KV dtype",
                       parents=[common])
    p.add_argument("--json", action="store_true", help="Dump raw JSON")

    # estimate
    p = sub.add_parser("estimate",
                       help="GET /estimate: precomputed speeds/presets/GPU samples; "
                            "--model also shows the server's recommended offload",
                       parents=[common])
    p.add_argument("--model", default=None, help="Only show a single registered model")
    p.add_argument("--json", action="store_true", help="Dump raw JSON")

    # endpoint
    p = sub.add_parser("endpoint",
                       help="Print OpenAI-compatible endpoint + secret for ChatOpenAI",
                       parents=[common])
    p.add_argument("--model", default=None,
                   help="Model key from saved state (default: first saved)")
    p.add_argument("--secret", default=None)
    p.add_argument("--endpoint", default=None, help="Override base URL")
    p.add_argument("--env", action="store_true",
                   help="Print as env-var settings for report agent")

    # chat
    p = sub.add_parser("chat",
                       help="Smoke-test the OpenAI-compatible /v1 proxy",
                       parents=[common])
    p.add_argument("--model", default=None,
                   help="Model key from saved state (default: first saved)")
    p.add_argument("--secret", default=None)
    p.add_argument("--endpoint", default=None,
                   help="Override base URL (eg direct llama-server)")
    p.add_argument("--message", default="Reply with exactly: pong")
    p.add_argument("--max-tokens", type=int, default=256,
                   help="Max reply tokens (default 256; raise e.g. --max-tokens 2048 "
                        "for full long answers)")
    p.add_argument("--temperature", type=float, default=0.2)

    args = parser.parse_args()
    {"status": cmd_status, "launch": cmd_launch, "stop": cmd_stop,
     "resources": cmd_resources, "estimate": cmd_estimate,
     "endpoint": cmd_endpoint, "chat": cmd_chat}[args.command](args)


if __name__ == "__main__":
    main()