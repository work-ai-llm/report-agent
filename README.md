# Report Agent

An agent that turns penetration testing findings into finished reports. It reads
raw evidence — nmap output, BloodHound dumps, uploaded notes — and writes a
structured report back to a persistent virtual filesystem, built section by
section so that tool output never overflows a single context window.

Exposed three ways: a Streamlit chat UI, an A2A server for agent-to-agent use,
and a set of operator CLIs.

## Quickstart

The path below assumes **no GPU and no local model server**: it uses OpenRouter's
free tier, which needs only an API key. For a local model server, see
[Local model server](#local-model-server).

**Requirements:** Python 3.11+, and an OpenRouter API key
(free — <https://openrouter.ai/settings/keys>).

```bash
git clone https://github.com/work-ai-llm/report-agent.git
cd report-agent
pip install -e .

cp .env.example .env
```

Edit `.env` and set one thing:

```bash
LLM_BACKEND=openrouter
OPENROUTER=sk-or-...
```

Leave `POSTGRES_URI` empty — with no reachable database the agent falls back to a
local SQLite file automatically.

Seed the report templates and interpretation guides, then launch:

```bash
python dev_tools/store_cli.py seed --dir agent/context
python -m streamlit_ui --agent real --backend sqlite --debug
```

The `--debug` banner prints which backend and which database are actually in use.
Check it before anything else — it settles most "why is it doing that" questions.

Then, in the browser:

1. **Sidebar → Upload** — add your findings files (`nmap.txt`, notes, whatever).
2. **Ask** — e.g. `What's in my findings, and what can you build from them?`
3. **Keep asking** — the report is assembled across several turns. Ask for the
   executive summary when the technical sections exist.
4. **Sidebar → Reports** — pick one and download it.

### Seeding is required

`agent/context/` holds the report templates and finding-interpretation guides, and
they are **not** loaded automatically — the agent only reads the store, never that
directory on disk. Without the `seed` step the agent finds no templates and has
nothing to follow.

Findings are not seeded either; they arrive through the uploader.

## Usage

Run in this order, one at a time.

**1. The UI**

```bash
python -m streamlit_ui                                  # real agent, Postgres
python -m streamlit_ui --backend sqlite                 # real agent, local SQLite
python -m streamlit_ui --agent mocks                    # full in-memory sandbox, no keys needed
python -m streamlit_ui --agent mock --backend sqlite    # real storage, scripted answers
python -m streamlit_ui --llm-backend openrouter         # pick the chat backend
```

Mode is fixed for the life of the process — you cannot switch backend or agent
mid-run. `--agent mocks` needs no API key at all, which is the fastest way to see
the interface work.

**2. The A2A server**

```bash
python agent/a2a_connector.py                       # loopback only
python agent/a2a_connector.py --host all_interfaces # also on LAN and tailnet
```

Binds loopback by default. `--host all_interfaces` exposes an **unauthenticated**
agent to the network — there is no auth either way. Drive it with:

```bash
python dev_tools/a2a_cli.py --server http://127.0.0.1:9999 list
python dev_tools/a2a_cli.py --server http://127.0.0.1:9999 send "build a report"
python dev_tools/a2a_cli.py --server http://127.0.0.1:9999 get --task <task_id>
```

Ctrl-C disconnects the client; the server keeps working.

**3. Local model server** (only for the `openai_local` backend)

```bash
python dev_tools/model_server_cli.py status
python dev_tools/model_server_cli.py launch --model ornith
python dev_tools/model_server_cli.py endpoint --env   # prints base URL + secret
```

Put the printed values in `.env` as `LLM_BASE_URL` and `LLM_API_KEY`, set
`LLM_BACKEND=openai_local`, and restart the app.

**4. Operator CLIs**

```bash
python dev_tools/store_cli.py [--sqlite] {seed,wipe,inspect,list-sessions,export,delete,inspect-session,reconcile-files}
```

Full reference, including the traps: [docs/dev-tools.md](docs/dev-tools.md).

## Configuration

Every environment variable, with defaults and comments, is in
[`.env.example`](.env.example). That file is the single source of truth — it is
the only place the table exists, so it cannot drift.

The essentials:

| Variable | |
|---|---|
| `LLM_BACKEND` | `openai_local`, `openrouter`, or `mistral`. Inferred from which keys exist if unset. |
| `AGENT_MODEL` | Optional model id. **Backend-agnostic** — leave it unset to use that backend's default, and it follows `LLM_BACKEND` automatically. |
| `POSTGRES_URI` | Leave empty for local SQLite. Falls back automatically if unreachable. |
| `LOG_LEVEL` | `NONE` (default), `INFO`, `FULL`, `DEBUG`. At `INFO` you get one line per LLM request. |

Changing backend is usually a one-line change: the model id defaults are per
backend, so switching `LLM_BACKEND` switches the model with it.

## Testing

```bash
pytest              # mock suite only — no services, no keys, ~35s
```

Two suites and a set of experiments, deliberately separate:

- **Mock** — `pytest`. The fast inner loop and the gate for UI and adapter
  changes. This is what CI should run.
- **Acceptance** — numbered scripts in `tests/acceptance/`, driven against live
  services and real keys. Run them by explicit path:

  ```bash
  pytest tests/acceptance/5_report_agent_structure.py
  ```

- **Experiment** — `tests/experiments/` scripts, run manually by path. The
  finding files in `results/findings/` that feed them are **synthetic data**,
  written by hand so fact retention can be measured against a known ground
  truth: fake domain, RFC 1918 and RFC 5737 addresses, invented share and file
  names. They are not engagement output, and nothing should be inferred about a
  real target from them.

Bare `pytest` collects only `tests/mock/test_*.py`. The numbered acceptance
scripts do not match pytest's discovery patterns and are never run automatically.

## What it does not do

Stated plainly, because these look available and are not.

- **No live token streaming.** Tool calls appear live; the answer arrives whole.
- **No auth on the A2A server.** Loopback by default, but `--host all_interfaces`
  exposes an unauthenticated agent.
- **No rate limiting applied.** See
  [architecture.md](docs/architecture.md#rate-limiting--present-but-not-wired).

## Architecture in one diagram

One agent runtime, two independent consumers that share nothing else:

```
             ┌────────────────────────────────────┐
             │  AgentRuntime                      │
             │  models · middleware · checkpointer│
             │  store · graph · tool              │
             └──────────────────┬─────────────────┘
                                │
          ┌─────────────────────┴─────────────────────┐
┌─────────▼──────────┐                  ┌───────────────▼──────────┐
│ A2A                │                  │ Streamlit                 │
│ server → client    │                  │ protocols → adapters →   │
│   library → CLI    │                  │   app                    │
└────────────────────┘                  └──────────────────────────┘
```

Detail: [docs/architecture.md](docs/architecture.md).

## Documentation

| | |
|---|---|
| [docs/overview.md](docs/overview.md) | What it is, what it does not do, how to run it |
| [docs/architecture.md](docs/architecture.md) | How it is built, invariants, traps |
| [docs/dev-tools.md](docs/dev-tools.md) | The operator CLIs |
| [docs/dependencies.md](docs/dependencies.md) | What each dependency is for |
| [docs/troubleshooting.md](docs/troubleshooting.md) | Symptom → cause |
| [docs/changelog.md](docs/changelog.md) | What changed when |
| [AGENTS.md](AGENTS.md) | Traps and invariants, for humans and agents |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Branch naming, PR shape |
| [docs/history/](docs/history/) | Parked PR journals. Provenance, not truth |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Each document owns one job and ends with
an "Update this when" list, so a change normally touches one doc rather than the
set.

## License

<!-- LICENSE: undecided - see issue #26. -->