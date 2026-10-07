# Overview

What this project is, what it does, and how to run it. For how it is built, see
[architecture.md](architecture.md); for what can go wrong, see
[troubleshooting.md](troubleshooting.md).

## What it is

An agent that turns pentest findings into finished reports.

It reads raw evidence — nmap output, BloodHound dumps, uploaded notes — from a
persistent virtual filesystem, and writes a structured report back to it. The
report is built section by section into a scratchpad rather than in one pass,
because tool output and file dumps routinely exceed a single context window.

The external system prompt lives in `agent/reports.md`. Finding-interpretation
guides and the report template live in `agent/context/`.

## The surfaces

One agent, exposed three ways:

| Surface | Entry point | For |
|---|---|---|
| Streamlit chat UI | `python -m streamlit_ui` | Interactive use. Upload findings, watch the agent work, download the report. |
| A2A server | `python agent/a2a_connector.py` | Another agent driving this one over a protocol. |
| Dev CLIs | `dev_tools/` | Operating the model server, the store, and the A2A client from a shell. See [dev-tools.md](dev-tools.md). |

The two user-facing surfaces share only the agent runtime. They have separate
state, separate session identity, and separate transports, and either can be
developed or broken without touching the other.

## LLM backends

Three, selected by `LLM_BACKEND`:

| Backend | For |
|---|---|
| `openai_local` | A local model server (llama.cpp or anything OpenAI-compatible). The default in `.env.example`. |
| `openrouter` | Hosted free-tier models. No local GPU needed. |
| `mistral` | The Mistral API. |

Only `openai_local` needs no external account. The other two need a key; see
`.env.example` for which variable each one reads.

Model ids default per backend, so switching `LLM_BACKEND` is usually the only
edit needed. See `AGENTS.md` for the two override variables and the trap around
leaving a stale one behind.

## Storage

Postgres when `POSTGRES_URI` is set and reachable; local SQLite otherwise. The
fallback is automatic and the timeouts are tuned so an unreachable database
degrades quickly rather than hanging startup.

`REPORT_AGENT_FORCE_SQLITE=1`, or `--backend sqlite`, forces the local path when
you want it deliberately.

## Observability

Langfuse, optional. If it is unreachable the agent runs with a no-op handler
rather than failing.

A session id is passed on every run so all turns of one thread land in a single
Langfuse session. Token counts are reported for the `openai_local` backend; see
[troubleshooting.md](troubleshooting.md) for why, and for what that looks like
when it is wrong.

## What it does not do

Stated plainly, because these are the things that look available and are not.

- **No live token streaming in the UI.** Tool calls appear live; the answer
  arrives whole. The `--streaming` / `--no-streaming` flags are plumbed and
  currently do nothing.
- **No authentication on the A2A server.** Loopback-only by default, but
  `--host all_interfaces` exposes an unauthenticated agent to the network.
- **No rate limiting is applied.** The limiter is built and deliberately not
  wired; see [architecture.md](architecture.md#rate-limiting--present-but-not-wired)
  for why.
- **Stopping a UI run does not cancel the agent.** The stream continues to
  completion. Tracked in #39.

## Running it

```bash
pip install -e .

# UI, offline, nothing else needed
python -m streamlit_ui --agent mocks

# UI, real agent, local SQLite
python -m streamlit_ui --agent real --backend sqlite

# UI against OpenRouter
python -m streamlit_ui --agent real --llm-backend openrouter --backend sqlite --debug

# A2A server, loopback only
python agent/a2a_connector.py
```

`--debug` prints a connectivity report before starting. Use it first whenever
something is not working — it settles which backend and which database are
actually in use.

## Layout

```
agent/          the agent runtime, the A2A server, the system prompt
client/         the A2A client library
streamlit_ui/   the chat UI
dev_tools/      operator CLIs
tests/          mock (fast), acceptance (live), experiments
docs/           this documentation
results/        sample findings, and generated reports
```

Per-file detail is in `AGENTS.md`. Per-layer detail is in
[architecture.md](architecture.md).

## Update this when

- a surface is added or removed, or its entry point changes
- the backend set, storage options, or observability behaviour changes
- a limitation above is fixed or a new one appears

Not for: implementation detail (that is `architecture.md`), env vars (that is
`.env.example`), or dependency purposes (that is `dependencies.md`).