"""
EXPERIMENT (category 3 in tests/tests.md) — does `stream_usage=True` actually
produce token counts against the LOCAL OpenAI-compatible backend?

WHY THIS FILE EXISTS
--------------------
`AgentRuntime._init_llm` (agent/report_agent.py) passes `stream_usage=True` to the
`ChatOpenAI` branch, and the comment above that line asserts:

    "Local llama.cpp servers honour include_usage and then report usage."

That assertion is UNVERIFIED. Nobody has ever watched the bytes come off the wire.
It was written from the shape of the langchain-openai code (it only auto-enables
stream_usage when base_url is the default OpenAI one) plus a belief about
llama.cpp. The belief might be wrong. A local server may simply ignore
`stream_options: {include_usage: true}` and the whole comment is fiction.

The consequence of it being wrong is quiet and expensive: every generation lands
in Langfuse with `promptTokens 0, usageDetails {}`, cost tracking is dead, and
nothing anywhere raises. That is exactly the failure mode already recorded on a
live trace (see AGENTS.md on Langfuse session grouping, and any generation in the
project with input=0/output=0).

So this script is deliberately an experiment and not a test:
  * it runs against LIVE services (model server + Langfuse),
  * it never asserts-and-die on an unreachable service — it prints UNVERIFIED,
  * it does not modify any tracked file, and
  * it does not fix anything it finds. Measure, print, exit.

WHAT IT MEASURES (numbered verdicts printed at the end)
------------------------------------------------------
  1. The production path works at all (full AgentRuntime.initialize() + agent run).
  2. The final AIMessage carries usage_metadata with input>0 AND output>0.
     ZERO TOKENS IS A FAIL, NOT A PASS — zero is precisely the bug under test.
  3. RAW WIRE EVIDENCE: the actual SSE bytes the SERVER sent. This is the valuable
     item. Captured by swapping the httpx transport underneath the already-built
     ChatOpenAI instances — the model object itself is untouched, so whatever we
     see is what production would have seen. Reports whether an
     include_usage-style chunk arrived at all, and prints it.
  4. LangChain itself saw the numbers: our own BaseCallbackHandler.on_llm_end,
     attached into `config["callbacks"]` next to the runtime's Langfuse handler.
     Proves the counts reached the LangChain layer regardless of any tracer.
  5. Langfuse recorded them: query the Langfuse read API for THIS run's trace and
     compare the generation's usage against what we measured.
  6. The verdict block.

CONTROL EXPERIMENT
------------------
Item 3b re-runs the same model call with `stream_usage=False`. If usage appears in
one and not the other, the flag is the cause. If usage appears in BOTH, the flag
is not what makes it work and the code comment is describing the wrong
mechanism. That distinction is the whole point of running the control.

HOW TO RUN
----------
    <venv python> tests/experiments/exp_token_usage.py

Filename deliberately does NOT match pytest's `test_*.py` / `*_test.py`
discovery patterns, so bare `pytest` (which collects only tests/mock/test_*.py,
see AGENTS.md) can never pick this up and never needs a live backend.

NEVER PRINTS SECRETS: no key values are echoed. The base URL is reduced to
scheme+host+port, credentials in URLs are stripped.
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Step 0: environment BEFORE any repo import.
#
# This ordering is load-bearing, not stylistic.
#   * `agent.report_agent` calls `load_dotenv()` at import and reads
#     `LOCAL_DB_PATH` at MODULE level (report_agent.py line ~122). If .env has
#     already been loaded by the time we set REPORT_AGENT_DB_PATH, the real
#     `agent/agent_session.db` gets opened. So we set it first.
#   * `REPORT_AGENT_FORCE_SQLITE=1` because unsetting POSTGRES_URI does not work
#     (load_dotenv puts it straight back) — the documented way to stay off
#     Postgres.
#   * LOG_LEVEL stays whatever .env says; this script prints with print(), so it
#     is unaffected by the logging tiers.
# ---------------------------------------------------------------------------
_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_ROOT))

_TMP_DB = Path(tempfile.gettempdir()) / f"exp_token_usage_{os.getpid()}.db"
os.environ["REPORT_AGENT_DB_PATH"] = str(_TMP_DB)
os.environ["REPORT_AGENT_FORCE_SQLITE"] = "1"

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # Windows console defaults to cp1252 and dies on the box-drawing chars below.
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Verdict accumulator. Every measurement appends here; nothing raises.
# The point of an experiment on a possibly-broken system is that it always
# produces a verdict, so each probe is wrapped independently.
# ---------------------------------------------------------------------------
VERDICTS = []


def verdict(item, state, detail=""):
    """state is exactly one of PASS / FAIL / UNVERIFIED."""
    VERDICTS.append((item, state, detail))
    print(f"  [{item}] {state:<10} {detail}", flush=True)


def head(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72, flush=True)


def safe_url(raw):
    """Reduce a URL to scheme://host:port/path. Never echoes userinfo or a key."""
    try:
        from urllib.parse import urlsplit
        p = urlsplit(raw or "")
        if not p.scheme or not p.netloc:
            return "<unset>"
        return f"{p.scheme}://{p.hostname}:{p.port or ''}{p.path}"
    except Exception:
        return "<unparseable>"


# ===========================================================================
# RAW WIRE TAP
# ===========================================================================
# Why a transport wrapper rather than an httpx event hook: `response` event hooks
# in httpx fire when headers arrive, not per body chunk, so they cannot see SSE
# payloads. Wrapping the transport lets us tee every byte chunk of a streaming
# response. We ALSO record the outgoing request body, which is how we prove
# `stream_options: {"include_usage": true}` actually went out on the wire.
#
# The OpenAI SDK (openai 3.x) talks HTTP through `httpx2`, and langchain-openai
# wraps its clients in `._client_utils._SyncHttpxClientWrapper` /
# `_AsyncHttpxClientWrapper` which are still httpx2 clients underneath. We only
# replace `_transport`, so base_url / auth / retries / timeouts stay exactly as
# the runtime configured them.
import httpx2


class _SyncTap(httpx2.SyncByteStream):
    def __init__(self, inner, sink):
        self.inner, self.sink = inner, sink

    def __iter__(self):
        for b in self.inner:
            self.sink.append(b)
            yield b

    def close(self):
        self.inner.close()


class _AsyncTap(httpx2.AsyncByteStream):
    def __init__(self, inner, sink):
        self.inner, self.sink = inner, sink

    async def __aiter__(self):
        async for b in self.inner:
            self.sink.append(b)
            yield b

    async def aclose(self):
        await self.inner.aclose()


class WireTap:
    """Collects request bodies + streaming response bytes for one chat model."""

    def __init__(self, label):
        self.label = label
        self.bodies = []       # outgoing JSON request bodies (as text)
        self.chunks = []       # raw SSE byte blobs, in arrival order
        self.sse_seen = 0      # how many SSE responses were tapped
        self.errors = []

    # -- parsing helpers ---------------------------------------------------
    def raw_text(self):
        return b"".join(self.chunks).decode("utf-8", "replace")

    def sse_events(self):
        """Parse the tapped bytes into decoded `data:` JSON objects."""
        out = []
        for line in self.raw_text().splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload or payload == "[DONE]":
                continue
            try:
                out.append(json.loads(payload))
            except Exception:
                pass
        return out

    def usage_events(self):
        return [e for e in self.sse_events() if isinstance(e.get("usage"), dict) and e["usage"]]

    def request_had_include_usage(self):
        for b in self.bodies:
            try:
                j = json.loads(b)
            except Exception:
                continue
            so = j.get("stream_options") or {}
            if so.get("include_usage") is True:
                return True
        return False


def attach_taps(chat_model, taps):
    """Install transport taps on BOTH the sync and async clients of a ChatOpenAI.

    Returns a list of human-readable labels for the clients we successfully
    tapped. Production's agent path is async, so the async tap is the one whose
    bytes matter; the sync tap is installed too so the control experiment can
    reuse the same instrumented model.
    """
    labelled = []
    for attr, tap in taps:
        try:
            root = getattr(chat_model, attr)
            http = getattr(root, "_client")
        except Exception as e:
            print(f"  (tap) no client for {attr}: {type(e).__name__}", flush=True)
            continue
        inner = getattr(http, "_transport", None)
        if inner is None:
            print(f"  (tap) {attr} has no _transport; skipped", flush=True)
            continue
        already = getattr(inner, "_exp_token_usage_wrapped", False)
        if already:
            labelled.append(f"{attr}:{type(inner).__name__}(already)")
            continue

        tap_obj = tap
        if hasattr(inner, "handle_async_request"):
            class _ATapTransport(httpx2.AsyncBaseTransport):
                _exp_token_usage_wrapped = True

                def __init__(self, inner, t):
                    self.inner, self.t = inner, t

                async def handle_async_request(self, request):
                    _record_request(tap_obj, request)
                    resp = await self.inner.handle_async_request(request)
                    if _is_sse(resp):
                        tap_obj.sse_seen += 1
                        resp.stream = _AsyncTap(resp.stream, tap_obj.chunks)
                    return resp

                async def aclose(self):
                    await self.inner.aclose()

            http._transport = _ATapTransport(inner, tap_obj)
            kind = "async"
        else:
            class _STapTransport(httpx2.BaseTransport):
                _exp_token_usage_wrapped = True

                def __init__(self, inner, t):
                    self.inner, self.t = inner, t

                def handle_request(self, request):
                    _record_request(tap_obj, request)
                    resp = self.inner.handle_request(request)
                    if _is_sse(resp):
                        tap_obj.sse_seen += 1
                        resp.stream = _SyncTap(resp.stream, tap_obj.chunks)
                    return resp

                def close(self):
                    self.inner.close()

            http._transport = _STapTransport(inner, tap_obj)
            kind = "sync"

        labelled.append(f"{attr}:{kind}:{type(http).__name__}")
    return labelled


def _record_request(tap, request):
    try:
        body = request.content
        if body:
            tap.bodies.append(body.decode("utf-8", "replace"))
    except Exception:
        # Streaming/file bodies are not readable here; irrelevant for chat.
        pass


def _is_sse(resp):
    try:
        return "text/event-stream" in (resp.headers.get("content-type") or "")
    except Exception:
        return False


# ===========================================================================
# LangChain-side callback (item 4)
# ===========================================================================
from langchain_core.callbacks import BaseCallbackHandler


class UsagePrinter(BaseCallbackHandler):
    """Prints what LANGCHAIN believes the token counts were.

    Deliberately independent of Langfuse: this is the ground truth for "did the
    numbers reach the LangChain layer", which the Langfuse verdict then has to
    agree with. If the two disagree, the bug is in the tracer, not the model.
    """

    def __init__(self):
        self.ends = []          # list of dicts, one per on_llm_end
        self.starts = 0

    def on_llm_start(self, serialized, prompts, **kwargs):
        self.starts += 1

    def on_llm_end(self, response, **kwargs):
        gen = None
        try:
            gens = response.generations or []
            if gens:
                gen = gens[0][0]
        except Exception:
            gen = None

        rec = {
            "llm_output_token_usage": None,
            "message_usage_metadata": None,
            "generation_message_usage_metadata": None,
            "finish_reason": None,
            "response_metadata_keys": [],
        }
        try:
            rec["llm_output_token_usage"] = (response.llm_output or {}).get("token_usage")
        except Exception:
            pass
        try:
            rec["finish_reason"] = (response.llm_output or {}).get("finish_reason")
        except Exception:
            pass
        try:
            msg = getattr(gen, "message", None)
            if msg is not None:
                rec["message_usage_metadata"] = getattr(msg, "usage_metadata", None)
                rec["generation_message_usage_metadata"] = getattr(
                    gen, "usage_metadata", None)
                md = getattr(msg, "response_metadata", None) or {}
                rec["response_metadata_keys"] = sorted(md.keys())
                tu = md.get("token_usage")
                if tu:
                    rec["response_metadata_token_usage"] = tu
        except Exception:
            pass
        self.ends.append(rec)
        print(f"  [cb on_llm_end #{len(self.ends)}] "
              f"llm_output.token_usage={rec['llm_output_token_usage']} | "
              f"message.usage_metadata={rec['message_usage_metadata']} | "
              f"response_metadata_keys={rec['response_metadata_keys']}", flush=True)


# ===========================================================================
# Helpers for the run
# ===========================================================================
SESSION = f"exp-token-usage-{int(time.time())}"


def tok(meta):
    """Pull (input, output) out of a usage_metadata dict, or (None, None)."""
    if not isinstance(meta, dict):
        return (None, None)
    return (meta.get("input_tokens"), meta.get("output_tokens"))


def last_ai_with_usage(messages):
    """Last AIMessage that carries a usage_metadata dict (even an empty one)."""
    for m in reversed(messages or []):
        if getattr(m, "type", None) == "ai" and isinstance(
                getattr(m, "usage_metadata", None), dict):
            return m
    for m in reversed(messages or []):
        if getattr(m, "type", None) == "ai":
            return m
    return None


# ===========================================================================
# MAIN
# ===========================================================================
async def main():
    # -----------------------------------------------------------------------
    # Import AFTER the env is set (see step 0 comment).
    # -----------------------------------------------------------------------
    head("0. ENVIRONMENT")
    from agent.report_agent import AgentRuntime  # noqa: E402
    from langchain_openai import ChatOpenAI  # noqa: E402

    print(f"  LLM_BACKEND      = {os.getenv('LLM_BACKEND')}")
    print(f"  LLM_BASE_URL     = {safe_url(os.getenv('LLM_BASE_URL'))}  (host/port only)")
    print(f"  LLM_API_KEY set  = {bool(os.getenv('LLM_API_KEY'))}  (value never printed)")
    print(f"  AGENT_MODEL      = {os.getenv('AGENT_MODEL') or '(backend default)'}")
    print(f"  DB (this run)    = {_TMP_DB}")
    print(f"  FORCE_SQLITE     = {os.getenv('REPORT_AGENT_FORCE_SQLITE')}")

    # -----------------------------------------------------------------------
    # Item 1 — build the model the way production does.
    # -----------------------------------------------------------------------
    head("1. PRODUCTION PATH: AgentRuntime.initialize()")
    rt = AgentRuntime(debug_mode=False)
    used_full_runtime = False
    try:
        await rt.initialize(debug_mode=False, force_sqlite=True)
        used_full_runtime = True
    except Exception as e:
        verdict(1, "UNVERIFIED",
                f"full AgentRuntime.initialize() failed: {type(e).__name__}: {str(e)[:200]}")
        print("  Falling back to the bare model factory "
              "(AgentRuntime._init_llm only).", flush=True)

    if used_full_runtime:
        verdict(1, "PASS", "AgentRuntime.initialize() succeeded; agent graph built")
        print(f"  Langfuse handler attached at runtime init: "
              f"{rt.langfuse_handler is not None} (langfuse_ok={rt.langfuse_ok})")
    else:
        rt._init_llm(streaming=True)

    llm = rt.llm
    llm_small = rt.llm_small
    print(f"  chat model   : {type(llm).__name__} "
          f"model={getattr(llm, 'model', None) or getattr(llm, 'model_name', None)}")
    print(f"  streaming    : {getattr(llm, 'streaming', None)}")
    print(f"  stream_usage : {getattr(llm, 'stream_usage', None)}")
    print(f"  emulator llm : {getattr(llm_small, 'model', None)} "
          f"(stream_usage={getattr(llm_small, 'stream_usage', None)})")
    if not used_full_runtime:
        verdict(1, "UNVERIFIED", "chat model built via _init_llm, agent graph not exercised")

    # -----------------------------------------------------------------------
    # Instrument the wire BEFORE any call happens.
    # -----------------------------------------------------------------------
    head("2-3. RAW WIRE TAP (installed on the real model objects)")
    tap_main = WireTap("main")
    tap_small = WireTap("emulator")
    labels = attach_taps(llm, [("root_client", tap_main), ("root_async_client", tap_main)])
    labels += attach_taps(llm_small, [("root_client", tap_small), ("root_async_client", tap_small)])
    for l in labels:
        print(f"  tapped {l}")
    if not labels:
        verdict(3, "UNVERIFIED", "could not install a transport tap on any HTTP client")

    printer = UsagePrinter()

    # -----------------------------------------------------------------------
    # Item 2 + 4 — a direct streaming call at the model level. Streaming (not
    # invoke) so we can also look at intermediate chunks, which is where a
    # usage chunk shows up: OpenAI puts it in the LAST chunk, AFTER the final
    # finish_reason, with empty content.
    # -----------------------------------------------------------------------
    head("2 + 4. DIRECT STREAMING CALL (usage_metadata + our own callback)")
    direct_meta = None
    direct_in = direct_out = None
    try:
        chunks = []
        async for ch in llm.astream("Reply with exactly the word: OK",
                                    config={"callbacks": [printer]}):
            chunks.append(ch)
        print(f"  streamed chunks: {len(chunks)}")
        with_usage = [c for c in chunks
                      if isinstance(getattr(c, "usage_metadata", None), dict)]
        print(f"  chunks carrying usage_metadata: {len(with_usage)} "
              f"(positions: {[chunks.index(c) for c in with_usage]})")
        if chunks:
            print(f"  final chunk: usage_metadata={chunks[-1].usage_metadata} "
                  f"finish={chunks[-1].response_metadata.get('finish_reason')}")
    except Exception as e:
        verdict(2, "FAIL", f"direct astream raised {type(e).__name__}: {str(e)[:200]}")

    # The aggregated message is the real production surface: what the agent
    # graph stores and what the final AIMessage carries.
    try:
        direct = await llm.ainvoke("Reply with exactly the word: OK",
                                   config={"callbacks": [printer]})
        direct_meta = direct.usage_metadata
        direct_in, direct_out = tok(direct_meta)
        print(f"  ainvoke usage_metadata = {direct_meta}")
        print(f"  ainvoke response_metadata = "
              f"{ {k: v for k, v in direct.response_metadata.items() if k != 'model_provider'} }")
    except Exception as e:
        verdict(2, "FAIL", f"ainvoke raised {type(e).__name__}: {str(e)[:200]}")

    # -----------------------------------------------------------------------
    # Item 3 — the wire evidence, printed in full.
    # -----------------------------------------------------------------------
    head("3. RAW WIRE: did the SERVER send a usage chunk?")
    for tap in (tap_main, tap_small):
        print(f"\n  --- tap[{tap.label}] ---")
        print(f"  outgoing request bodies captured : {len(tap.bodies)}")
        print(f"  SSE responses intercepted         : {tap.sse_seen}")
        print(f"  include_usage=True on the wire    : {tap.request_had_include_usage()}")
        for b in tap.bodies:
            try:
                j = json.loads(b)
                print(f"    -> model={j.get('model')!r} stream={j.get('stream')} "
                      f"stream_options={j.get('stream_options')}")
            except Exception:
                print("    -> <unparseable body>")
        evs = tap.sse_events()
        usage_evs = tap.usage_events()
        print(f"  decoded SSE `data:` objects       : {len(evs)}")
        print(f"  of those, WITH a `usage` object   : {len(usage_evs)}")
        for e in usage_evs:
            print(f"    !! USAGE CHUNK FROM SERVER: {json.dumps(e['usage'])}")
        tail = tap.raw_text()[-600:]
        if tail:
            print("  --- last 600 bytes of the raw SSE body ---")
            for line in tail.splitlines():
                print(f"    | {line}")

    wire_usage = tap_main.usage_events()
    if tap_main.sse_seen == 0:
        verdict(3, "UNVERIFIED", "no SSE response was intercepted (see tap output)")
    elif wire_usage:
        u = wire_usage[-1]["usage"]
        verdict(3, "PASS",
                f"server DID send a usage chunk: prompt_tokens={u.get('prompt_tokens')} "
                f"completion_tokens={u.get('completion_tokens')} "
                f"total={u.get('total_tokens')}")
    else:
        verdict(3, "FAIL",
                f"server sent {len(tap_main.sse_events())} SSE chunks and NOT ONE "
                f"contained a usage object; include_usage was "
                f"{tap_main.request_had_include_usage()} on the wire")

    if not tap_main.request_had_include_usage() and tap_main.sse_seen:
        print("\n  NOTE: include_usage was NOT on the outgoing body. langchain-openai "
              "sent no stream_options at all, so the server was never asked — that is "
              "the mechanism the code comment describes, and it is separate from "
              "whether the server would have honoured it.")

    # -----------------------------------------------------------------------
    # Item 3b — CONTROL. Same server, same model id, stream_usage=False.
    # Isolates the flag from the server.
    # -----------------------------------------------------------------------
    head("3b. CONTROL: identical call with stream_usage=False")
    ctrl_tap = WireTap("control")
    try:
        ctrl = ChatOpenAI(
            model=getattr(llm, "model", None) or getattr(llm, "model_name", None),
            base_url=os.getenv("LLM_BASE_URL"),
            api_key=os.getenv("LLM_API_KEY", "none"),
            temperature=0.2,
            streaming=True,
            stream_usage=False,      # <-- the only difference
            timeout=180.0,
            rate_limiter=None,
            max_retries=3,
        )
        attach_taps(ctrl, [("root_client", ctrl_tap), ("root_async_client", ctrl_tap)])
        ctrl_msg = await ctrl.ainvoke("Reply with exactly the word: OK")
        ci, co = tok(ctrl_msg.usage_metadata)
        print(f"  control usage_metadata   = {ctrl_msg.usage_metadata}")
        print(f"  control SSE chunks       = {len(ctrl_tap.sse_events())}")
        print(f"  control usage chunks     = {len(ctrl_tap.usage_events())}")
        print(f"  control include_usage    = {ctrl_tap.request_had_include_usage()}")
        # Semantics of the control verdict are INVERTED on purpose. We WANT the
        # control to have no usage: that is what proves `stream_usage=True` is
        # load-bearing rather than the server volunteering usage regardless. So
        # "FAIL" here means "the flag is not doing anything", which is a
        # surprising and important outcome, not a script failure.
        got_usage = (ci or 0) > 0 and (co or 0) > 0
        verdict("3b", "FAIL" if got_usage else "PASS",
                f"control stream_usage=False -> input={ci} output={co}, "
                f"usage chunks on wire={len(ctrl_tap.usage_events())}; "
                f"expected NO usage, so the flag is proven load-bearing"
                if not got_usage else
                f"control ALSO returned usage (input={ci} output={co}) — the flag is "
                f"NOT what makes usage appear; the server sends it regardless")
    except Exception as e:
        verdict("3b", "UNVERIFIED", f"control call failed: {type(e).__name__}: {str(e)[:160]}")

    # -----------------------------------------------------------------------
    # Item 2 verdict, now that the direct call is measured.
    # -----------------------------------------------------------------------
    if direct_in is None and direct_out is None:
        verdict(2, "FAIL", "no usage_metadata at all on the final AIMessage")
    elif direct_in > 0 and direct_out > 0:
        verdict(2, "PASS", f"input_tokens={direct_in} output_tokens={direct_out} "
                           f"total={direct_meta.get('total_tokens')}")
    else:
        verdict(2, "FAIL", f"usage_metadata present but ZERO tokens: "
                           f"input={direct_in} output={direct_out} — this is the bug")

    # -----------------------------------------------------------------------
    # Item 1/2/4 via the REAL agent graph (production path), if available.
    # -----------------------------------------------------------------------
    head("1-4. AGENT GRAPH RUN (production path)")
    agent_meta = None
    if used_full_runtime:
        config = {
            "configurable": {"thread_id": SESSION, "session_id": SESSION},
            # commit 79545bc: this is the key that groups a session in Langfuse,
            # NOT "session.id". We need it so item 5 can find this run's trace.
            "metadata": {"langfuse_session_id": SESSION},
            "recursion_limit": 25,
            "callbacks": [printer],
        }
        cb_before = len(printer.ends)
        try:
            result = await rt.agent.ainvoke(
                {"messages": [{"role": "user",
                               "content": "hi — reply with one short sentence and use no tools."}]},
                config=config,
            )
            msgs = result.get("messages", [])
            ai = last_ai_with_usage(msgs)
            agent_meta = getattr(ai, "usage_metadata", None) if ai else None
            ai_in, ai_out = tok(agent_meta)
            print(f"  agent produced {len(msgs)} messages; last AI content: "
                  f"{str(getattr(ai, 'content', ''))[:120]!r}")
            print(f"  agent last AIMessage usage_metadata = {agent_meta}")
            if ai_in is not None and ai_in > 0 and ai_out > 0:
                verdict("2(agent)", "PASS", f"input={ai_in} output={ai_out}")
            else:
                verdict("2(agent)", "FAIL",
                        f"input={ai_in} output={ai_out} (zero/absent usage on the agent path)")
            new_ends = printer.ends[cb_before:]
            if new_ends:
                got = new_ends[-1]
                verdict(4, "PASS" if got["message_usage_metadata"] else "FAIL",
                        f"{len(new_ends)} llm_end event(s) during the agent run; last "
                        f"usage_metadata={got['message_usage_metadata']}")
            else:
                verdict(4, "FAIL", "our callback received no on_llm_end during the agent run")
        except Exception as e:
            verdict("1(agent)", "FAIL",
                    f"agent.ainvoke raised {type(e).__name__}: {str(e)[:250]}")
    else:
        verdict("1(agent)", "UNVERIFIED", "full runtime unavailable, agent graph not exercised")

    # -----------------------------------------------------------------------
    # Item 4 verdict for the direct calls if the agent run did not cover it.
    # -----------------------------------------------------------------------
    if not used_full_runtime and printer.ends:
        got = printer.ends[-1]
        verdict(4, "PASS" if got["message_usage_metadata"] else "FAIL",
                f"{len(printer.ends)} on_llm_end event(s); last "
                f"usage_metadata={got['message_usage_metadata']}")
    elif not used_full_runtime:
        verdict(4, "UNVERIFIED", "no on_llm_end fired")

    # -----------------------------------------------------------------------
    # Item 5 — Langfuse read path.
    #
    # langfuse 4.9.0 exposes NO public `.api` on the client object; the read
    # clients hang off the resource manager: `get_client()._resources.api`.
    # `observations.get_many` is v2-only and this server answers 404
    # ("observations v2 API is only available in a Langfuse v4 write mode"), so
    # the working read path is:
    #     api.trace.list(session_id=...)  ->  api.trace.get(trace_id)
    # TraceWithFullDetails carries `.observations`, each with `.usage`.
    # -----------------------------------------------------------------------
    head("5. LANGFUSE READ API")
    try:
        from langfuse import get_client as _get_client
        lc = _get_client()
        if not lc.auth_check():
            verdict(5, "UNVERIFIED", "Langfuse auth_check() returned False")
        else:
            print("  auth_check: True")
            api = lc._resources.api
            lc.flush()
            time.sleep(3)          # ingestion is async; give the queue a moment

            traces = None
            for attempt in range(5):
                res = api.trace.list(session_id=SESSION, limit=10)
                traces = [t for t in (res.data or []) if t.id]
                if traces:
                    break
                print(f"  trace not visible yet (attempt {attempt + 1}/5), waiting...")
                time.sleep(4)
                lc.flush()

            if not traces:
                verdict(5, "UNVERIFIED",
                        f"Langfuse reachable and authenticated, but no trace with "
                        f"session_id={SESSION!r} appeared after 5 polls. The run may "
                        f"not have been ingested yet, or the session key did not group.")
            else:
                trace = traces[0]
                print(f"  trace {trace.id} name={trace.name!r} session={trace.session_id!r}")
                detail = api.trace.get(trace.id)
                gens = [o for o in (detail.observations or [])
                        if str(getattr(o, "type", "")).upper() == "GENERATION"]
                print(f"  observations: {len(detail.observations or [])}, "
                      f"generations: {len(gens)}")
                nonzero = []
                for o in gens:
                    u = getattr(o, "usage", None)
                    ui = getattr(u, "input", None) if u else None
                    uo = getattr(u, "output", None) if u else None
                    print(f"    GENERATION name={getattr(o, 'name', None)!r} "
                          f"model={getattr(o, 'model', None)!r} usage input={ui} output={uo}")
                    if (ui or 0) > 0 and (uo or 0) > 0:
                        nonzero.append((ui, uo))
                if nonzero:
                    verdict(5, "PASS",
                            f"trace {trace.id}: {len(nonzero)}/{len(gens)} generation(s) "
                            f"carry non-zero usage, first = input {nonzero[0][0]} / "
                            f"output {nonzero[0][1]}")
                else:
                    verdict(5, "FAIL",
                            f"trace {trace.id}: {len(gens)} generation(s), all with "
                            f"input=0/output=0 — Langfuse recorded nothing")
    except Exception as e:
        verdict(5, "UNVERIFIED",
                f"Langfuse read path failed: {type(e).__name__}: {str(e)[:300]}")

    # -----------------------------------------------------------------------
    # Cleanup the temp DB. Never touch agent/agent_session.db.
    # -----------------------------------------------------------------------
    # Close the two aiosqlite connections the runtime opened BEFORE deleting the
    # file. They are worker threads with pending futures; leaving them open makes
    # them call back into an already-closed loop at interpreter shutdown, which
    # prints an alarming "Event loop is closed" traceback that has nothing to do
    # with the experiment. Closing here keeps the output readable, and the delete
    # keeps us from leaving anything behind.
    for attr in ("checkpointer", "store"):
        obj = getattr(rt, attr, None)
        conn = getattr(obj, "conn", None)
        if conn is not None:
            try:
                await conn.close()
            except Exception:
                pass
    try:
        rt.pool = None
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(_TMP_DB) + suffix)
            if p.exists():
                p.unlink()
    except Exception:
        pass

    # -----------------------------------------------------------------------
    # Item 6 — verdict block.
    # -----------------------------------------------------------------------
    head("6. VERDICT SUMMARY")
    # Probes fired in execution order, which is not numbered order. Sort by the
    # leading number so the summary reads the way the report does.
    def _key(v):
        num = "".join(ch for ch in str(v[0]).split("(")[0] if ch.isdigit())
        return (int(num) if num else 99, str(v[0]))

    for item, state, detail in sorted(VERDICTS, key=_key):
        print(f"  {str(item):<9} {state:<11} {detail}")

    print("\n  --- claim under test ---")
    print('  code comment: "Local llama.cpp servers honour include_usage and then report usage."')
    if any(tap_main.usage_events()):
        print("  VERDICT ON COMMENT: SUPPORTED — the server emitted a usage chunk.")
    elif tap_main.sse_seen and not tap_main.request_had_include_usage():
        print("  VERDICT ON COMMENT: UNVERIFIED — the server was never asked "
              "(no stream_options on the wire), so nothing is proven either way.")
    else:
        print("  VERDICT ON COMMENT: CONTRADICTED — the server was asked and did "
              "not send usage.")
    print(f"  measured on {datetime.now(timezone.utc).isoformat()} "
          f"against {safe_url(os.getenv('LLM_BASE_URL'))}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        sys.exit(130)
