# AGENTS.md

Orientation for coding agents and humans working in this repo. Read it before
changing code. It records **decisions and traps** — the things you cannot infer
by reading the source — not feature descriptions.

If a statement here contradicts the code, the code wins and this file is stale.
Fix it in the same change.

## What this is

A LangChain/LangGraph agent that turns pentest findings into reports, exposed
three ways: a Streamlit chat UI, an A2A server, and a set of dev CLIs. Python
3.11+.

## Where things are documented

Read the right one; several of these are history, not truth.

| File | Answers |
|---|---|
| `AGENTS.md` (this) | How to run it, what breaks, what not to touch |
| `.env.example` | Every env var name and its default. **Single source of truth** |
| `CONTRIBUTING.md` | Branch naming, PR shape, prose rules |
| `docs/overview.md` | Feature tour and limitations (still Mistral-centric) |
| `docs/architecture.md` | Component sketch (stale — predates the UI and multi-backend) |
| `tests/tests.md` | Why the suite is split in two |
| `docs/PR-a2a-client.md`, `docs/PR-a2a-acceptance.md` | **History.** Parked-work notes, not current design |

Do not restate the env-var table anywhere. Link to `.env.example` instead, so
there is exactly one place to update.

## Commands

```bash
pip install -e .

python -m streamlit_ui                                  # UI: real agent, Postgres
python -m streamlit_ui --backend sqlite                 # UI: real agent, local SQLite
python -m streamlit_ui --agent mock --backend sqlite    # real persistence, mock agent
python -m streamlit_ui --agent mocks                    # full in-memory sandbox
python -m streamlit_ui --debug                          # print connectivity report first

python agent/a2a_connector.py                           # A2A server, loopback only
python agent/a2a_connector.py --host all_interfaces      # A2A server on LAN + tailnet too
python dev_tools/a2a_cli.py --server http://127.0.0.1:9999 send "build a report"
python dev_tools/store_cli.py [--sqlite] {seed,wipe,inspect,list-sessions,export,delete,inspect-session,reconcile-files}
python dev_tools/model_server_cli.py {status,launch,stop,resources,estimate,endpoint,chat}

pytest                                                  # mock suite only — see Tests
```

The launcher flags are sticky: they set env vars and the chosen mode is fixed for
the life of the process. You cannot switch backend or agent mid-run.

## The four layers

1. **`agent/report_agent.py`** — `AgentRuntime` owns everything: the chat models,
   the middleware stack, the checkpointer + store, and the agent graph. All heavy
   init happens in `initialize()`, never at import.
2. **`agent/a2a_connector.py`** — A2A server. `ReportA2AExecutor` turns one A2A
   task into one agent invocation, with a global lock serialising execution for
   rate-limit safety.
3. **`client/a2a_client.py`** — full task-lifecycle A2A client. `dev_tools/a2a_cli.py`
   is its CLI surface.
4. **`streamlit_ui/`** — the UI, in four files worth knowing: `app.py` renders,
   `services/agent_service.py` holds logic, `services/interfaces.py` defines the
   three protocols, `services/real_backend.py` adapts the real agent. Mocks live
   in `tests/mock/mock_objects_streamlit.py` and satisfy the same protocols.

## Invariants

Break one of these and you lose data silently, not loudly.

- **One model factory.** Every chat model in the repo is constructed in
  `AgentRuntime._init_llm`. Adding a provider or changing model selection happens
  there. Nothing else builds a model.
- **The agent owns checkpoint namespace `""`.** The UI keeps its display history in
  `checkpoint_ns="ui"` under a fixed checkpoint id, tagged with metadata
  `{"source": "ui"}`. Writing UI rows into `""` wipes the agent's own state.
- **Store keys are slash-prefixed.** Virtual filesystem entries live in namespace
  `("filesystem",)` with keys like `/findings/nmap.txt` and values shaped
  `{"content": str, "encoding": "utf-8"}`. The UI and the agent must agree exactly
  or uploads become invisible to the agent.
- **Sync UI, async agent.** Streamlit runs synchronous scripts; LangGraph is async.
  Every async call crosses the process-wide `EventLoopThread`. Never call an
  awaitable directly from UI code.
- **Sessions are created on first message, not on page load.** That is deliberate —
  it keeps empty and interrupted sessions out of the selector.

## Traps

Each of these cost real debugging time. They are not obvious from the code.

- `ChatOpenRouter` takes its timeout in **milliseconds**; `_init_llm` multiplies by
  1000 for it. That looks like a bug and is not.
- Checkpoint blob versions must strictly increase per
  `(thread_id, checkpoint_ns, channel)`. The upsert is `ON CONFLICT DO NOTHING`, so
  reusing a version silently keeps the old blob — the "messages disappear" bug.
  Always derive the next version from the current checkpoint's `channel_versions`;
  a per-instance counter resets and collides.
- `list_threads` stays filtered (`{"source": "ui"}`, `limit=1000`). An unfiltered
  `alist(None)` on Postgres de-blobs every checkpoint in every namespace and
  exceeded the 60s bridge timeout, crashing the app at boot.
- The SQLite backend opens **two** aiosqlite connections to one file with WAL and
  `busy_timeout=30000`, and those values must stay in sync with
  `dev_tools/store_cli.py`. Diverge and the sync CLI hits "database is locked".
- To force local SQLite use `REPORT_AGENT_FORCE_SQLITE=1`. Unsetting
  `POSTGRES_URI` in the shell does not work — `load_dotenv` puts it right back.
- Postgres connect timeouts are deliberately tiny (`connect_timeout=3`, and the pool
  open is wrapped in a 4s `wait_for`) so offline falls back fast. Raising them makes
  boot hang instead of degrading.
- On Windows, `report_agent.py` and `real_backend.py` both set
  `WindowsSelectorEventLoopPolicy`. Removing either breaks async Postgres on win32.
- `stream_mode=["messages", "updates"]` does **not** work with this graph and
  middleware stack: the messages channel yields only `(chunk, metadata)` tuples, so
  structured `tool_call` / `tool_result` events never arrive. The UI depends on plain
  node-diff streaming. Tracked in #17 and #19 — do not retry it blind.
- Langfuse groups a turn into one trace only when `session_id` is present in
  `config["configurable"]`. Without it every model call becomes its own root trace.
  Tracked in #22; the A2A server already solved this by threading context id through.
- `STREAMLIT_STREAM` / `--no-streaming` is plumbed but **inert** — live token
  streaming is not implemented. Tool calls appear live; the answer arrives whole.

## Tests

Two suites, deliberately separate (see `tests/tests.md`).

- Mock — `pytest`, no external services and no keys. This is the fast inner loop
  and the gate for UI and adapter changes.
- Acceptance — numbered scripts in `tests/acceptance/`, driven against live
  services. Run them by explicit path.

Two things that will waste your time otherwise:

- Bare `pytest` collects **only** `tests/mock/test_*.py`. The numbered acceptance
  scripts do not match pytest's discovery patterns and are never run automatically.
- `tests/acceptance/1_test_agent.py` and `4_tailscale_db.py` still import
  `langchain_agent`, which no longer exists — it became `AgentRuntime` in the
  August refactor. They are stale. Their failures are not regressions.

## Configuration

`.env.example` lists every variable with defaults. Grouping, for orientation only:

- LLM backend selection — `LLM_BACKEND` plus per-backend model ids; falls back to
  inferring the backend from which keys are present.
- Provider credentials — local OpenAI-compatible server, OpenRouter, or Mistral.
- Observability — Langfuse, optional, degrades to no-op when unreachable.
- Storage — `POSTGRES_URI`, with local SQLite as the fallback.
- A2A — the server's bind address is the `--host` flag, two presets only:
  `local` (default) binds loopback, `all_interfaces` binds every NIC and is what
  survives a Wi-Fi IP change. `A2A_BIND` is the escape hatch for a specific
  address, and `A2A_HOST` only overrides the IP advertised in the agent card.
- Logging — `LOG_LEVEL`, honoured at import.

## Working agreements

- Branch `pr-<feature>` off `master`. Park large work as a Draft PR; the body is
  three short paragraphs: the idea, the current state, the direction. See
  `CONTRIBUTING.md` for the prose rules and the pre-review checklist.
- Never commit `.env` or any key. `.env.example` carries placeholders only.
- Keep this file to orientation and traps. Feature descriptions belong in `docs/`,
  and the docs that freeze current truth (README, architecture) are written **after**
  the program settles — tracked in #26 and #27.

## When to update this file

Update it in the same commit as the code, when a change does any of:

- adds, removes, or renames a command or env var
- adds or moves an invariant
- introduces a trap worth remembering
- retires something listed here as broken

You do **not** need to update it for ordinary feature work. That restraint is the
whole reason this file stays readable.