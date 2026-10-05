"""Live check for the #13 credential refresh against a real model server.

Run directly (not collected by pytest):

    python tests/experiments/exp_credential_refresh_live.py

Unlike exp_credential_refresh.py, which uses fake clients, this one puts a real
HTTP request through a real OpenAI-compatible endpoint so the 401 is genuine.

It uses a TEMP .env rather than the repo one, so the developer's own .env is
never touched, while LLM_BASE_URL points at the live server. That combination
exercises the real auth path without needing a real rotation.

Phases:
  1. build the runtime holding a deliberately wrong key
  2. call the model -> expect a real 401
  3. rewrite the temp .env with the server's actual key, as a user would after
     a model-server relaunch
  4. call again -> expect the refresh to fire and the request to succeed,
     with no process restart
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

WRONG_KEY = "deliberately-wrong-key"
PROMPT = "Reply with the single word: ok"


def _write_env(path: Path, key: str) -> None:
    path.write_text(
        "\n".join(
            [
                "LLM_BACKEND=openai_local",
                f"LLM_BASE_URL={os.environ['LLM_BASE_URL']}",
                f"LLM_API_KEY={key}",
                "",
            ]
        ),
        encoding="utf-8",
    )


async def _stream(runtime):
    """Stream one completion, letting any error propagate to the caller."""
    from langchain_core.messages import HumanMessage

    chunks = [
        c async for c in runtime.llm.astream([HumanMessage(content=PROMPT)])
    ]
    return "".join(str(getattr(c, "content", "") or "") for c in chunks)


async def _call(runtime) -> str:
    """Same as _stream, but reports failures instead of raising."""
    try:
        text = await _stream(runtime)
    except Exception as exc:
        return f"{type(exc).__name__}: {str(exc)[:120]}"
    return f"OK {text.strip()[:60]!r}"


async def main() -> int:
    from agent import report_agent as ra
    from agent.report_agent import CredentialRefreshRetryMiddleware

    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    real_key = os.environ.get("LLM_API_KEY", "").strip()
    if not base_url or not real_key:
        print("Set LLM_BASE_URL and LLM_API_KEY to the live server's values first.")
        print("  python dev_tools/model_server_cli.py launch --model ornith")
        print("  python dev_tools/model_server_cli.py endpoint --env")
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        _write_env(env_path, WRONG_KEY)

        real_find = ra.find_dotenv
        ra.find_dotenv = lambda *a, **k: str(env_path)

        os.environ["LLM_BACKEND"] = "openai_local"
        os.environ["LLM_BASE_URL"] = base_url
        os.environ["LLM_API_KEY"] = WRONG_KEY
        for stale in ("AGENT_MODEL", "AGENT_EMULATOR_MODEL"):
            os.environ.pop(stale, None)

        runtime = ra.AgentRuntime()
        runtime._init_llm()
        print(f"endpoint : {base_url}")
        print(f"client key at startup: {runtime.llm.root_client.api_key!r} (wrong on purpose)\n")

        print("1. call with the wrong key - expect a real 401")
        result = await _call(runtime)
        print(f"   -> {result}\n")
        saw_401 = "401" in result or "Authentication" in result or "Unauthorized" in result
        print(f"   401 observed: {saw_401}\n")

        print("2. the server's real key lands in .env (as after a relaunch)")
        _write_env(env_path, real_key)
        print("   .env updated; the process was NOT restarted\n")

        print("3. call again - the middleware should refresh and recover")
        refresh_calls = []

        async def handler(request):
            # Must let the error propagate: the middleware can only react to a
            # 401 it actually receives.
            return await _stream(runtime)

        mw = CredentialRefreshRetryMiddleware(
            refresh=lambda: bool(refresh_calls.append(1))
            or runtime._reload_llm_credential(),
            max_retries=3,
            retry_on=(Exception,),
            on_failure="raise",
            initial_delay=0.5,
            backoff_factor=2.0,
            max_delay=5.0,
            jitter=False,
        )
        try:
            # On success the handler's own return value comes back; only a
            # failure is wrapped into a ModelResponse holding an error message.
            response = await mw.awrap_model_call(object(), handler)
            items = getattr(response, "result", None)
            if items:
                outcome = "OK " + str(getattr(items[0], "content", items[0]) or "")[:60]
            else:
                outcome = "OK " + str(response or "")[:60]
        except Exception as exc:
            outcome = f"{type(exc).__name__}: {str(exc)[:120]}"
        print(f"   -> {outcome}")
        print(f"   key now: {runtime.llm.root_client.api_key!r}")
        print(f"   refresh attempts: {len(refresh_calls)}\n")

        ra.find_dotenv = real_find

    recovered = outcome.startswith("OK")
    print()
    print(f"401 seen without a restart : {saw_401}")
    print(f"recovered after .env edit  : {recovered}")
    if saw_401 and recovered:
        print("\nPASS - a rotated key no longer needs a process restart")
        return 0
    print("\nFAIL - see the output above")
    return 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))