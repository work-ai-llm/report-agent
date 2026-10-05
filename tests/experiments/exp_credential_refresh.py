"""Manual check for the #13 credential refresh, against a real temp .env file.

Run directly (not collected by pytest - the filename has no test_ prefix on
purpose, matching tests/experiments/):

    python tests/experiments/exp_credential_refresh.py

Simulates the real sequence: a process starts with one key, .env is then edited
to a new one, and a model call fails with a 401. Checks that the live client
picks up the new key and that no restart is needed.
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _write_env(path: Path, key: str) -> None:
    path.write_text(
        "\n".join(
            [
                "LLM_BACKEND=openai_local",
                "LLM_BASE_URL=http://localhost:1234/v1",
                f"LLM_API_KEY={key}",
                "",
            ]
        ),
        encoding="utf-8",
    )


def main() -> int:
    from agent import report_agent as ra

    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        _write_env(env_path, "KEY-BEFORE-ROTATION")

        # find_dotenv() walks up from the caller's file, so point it at the temp
        # dir for the duration of this check.
        real_find = ra.find_dotenv
        ra.find_dotenv = lambda *a, **k: str(env_path)

        os.environ["LLM_BACKEND"] = "openai_local"
        os.environ["LLM_BASE_URL"] = "http://localhost:1234/v1"
        os.environ["LLM_API_KEY"] = "KEY-BEFORE-ROTATION"
        for stale in ("AGENT_MODEL", "AGENT_EMULATOR_MODEL"):
            os.environ.pop(stale, None)

        runtime = ra.AgentRuntime()
        runtime._init_llm()

        failures = []

        def check(label: str, got, want) -> None:
            ok = got == want
            print(f"  {'PASS' if ok else 'FAIL'}  {label}: {got!r}")
            if not ok:
                failures.append(f"{label}: got {got!r}, want {want!r}")

        print("1. key as captured at startup")
        check("llm", runtime.llm.root_client.api_key, "KEY-BEFORE-ROTATION")

        print("2. .env edited underneath the running process")
        _write_env(env_path, "KEY-AFTER-ROTATION")
        # The live environment still holds the old value; only the file moved.
        check("os.environ untouched", os.environ["LLM_API_KEY"], "KEY-BEFORE-ROTATION")

        print("3. refresh after an auth failure")
        check("refresh reports a change", runtime._reload_llm_credential(), True)
        check("llm", runtime.llm.root_client.api_key, "KEY-AFTER-ROTATION")
        check("llm_small", runtime.llm_small.root_client.api_key, "KEY-AFTER-ROTATION")
        check(
            "outgoing header",
            runtime.llm.root_client.auth_headers.get("Authorization"),
            "Bearer KEY-AFTER-ROTATION",
        )

        print("4. no spurious update when nothing changed")
        check("refresh is a no-op", runtime._reload_llm_credential(), False)

        print("5. mistral is left alone (no root_client handle)")
        runtime.llm_backend = "mistral"
        check("mistral returns False", runtime._reload_llm_credential(), False)

        ra.find_dotenv = real_find

    print()
    if failures:
        print(f"{len(failures)} check(s) failed:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())