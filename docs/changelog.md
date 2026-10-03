# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and the
project intends to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**On the version labels below:** the repository has no git tags and no GitHub
releases. These blocks are reconstructed from `master` history and mark the two
periods of work, not published releases. The first real release is still to come.
Commit dates and short SHAs are given so any entry can be traced back.

## [Unreleased]

After the initial agent (2026-08-28 → 2026-10-03).

### Added

- **Streamlit UI** — full chat application with session selector, findings upload,
  report download, and delete. Supports three run modes: full real agent, real
  persistence with a mock agent, and a fully in-memory mock sandbox. (2026-10-02,
  `0ed358f`)
- **Protocol-based service layer** — `PenTestAgentService` with three injected
  dependencies (checkpointer, file store, agent runner), so the UI runs against
  mocks or the real agent without changing a line of UI code. (2026-10-02)
- **Dedicated event-loop bridge** — a persistent background asyncio thread lets
  synchronous Streamlit scripts drive the async LangGraph agent and its database
  connections. (2026-10-02)
- **Runtime connectivity report** — a plain-text banner, printed before the server
  starts, stating whether the checkpointer, virtual filesystem store, LLM API,
  agent object, and observability are actually connected. (2026-10-02)
- **Module launcher** — `python -m streamlit_ui` with `--backend`, `--agent`,
  `--debug`, and `--streaming` flags that translate to env vars. (2026-10-02)
- **Full-lifecycle A2A client** — one `A2AClient` covering send, streaming send,
  subscribe, get, list, cancel, and polling, plus a CLI wrapping all of it.
  (2026-09-24, `ccd8f66`, PR #10)
- **Model server CLI** — launch, stop, inspect, estimate, and smoke-test local
  models. (2026-09-14, `d366d82`)
- **Multi-backend LLM support** — local OpenAI-compatible server, OpenRouter, and
  Mistral behind a single `LLM_BACKEND` selector, with per-backend default model
  ids and acceptance tests against the live API. (2026-09-15, `60f1fb7`)
- **Runtime escape hatches** — a debug mode that swaps in a tool emulator, and a
  force-SQLite mode for offline work. (2026-09-15)
- **Store CLI growth** — session listing that works against Postgres, and a
  `reconcile-files` command that normalises store keys to the agent's slash form.
  (2026-09-16, `0ed358f`)
- **Docs** — PR conventions and a three-paragraph PR template (`CONTRIBUTING.md`,
  2026-09-08); `AGENTS.md` as the orientation and trap reference for agents
  (2026-10-03).

### Changed

- Storage framing is now Postgres-first with an automatic local SQLite fallback,
  rather than Postgres-only. The README env table was corrected to match.
  (2026-08-28, `05399f6`)
- Acceptance and mock test suites are documented as two deliberate layers, with the
  reasoning recorded in `tests/tests.md`. (2026-08-28, `e7318f0`)

### Fixed

- Bytecode is no longer tracked in git. (2026-09-25, `d2d2046`)
- A duplicated function definition was removed from the agent runtime. (2026-10-03,
  `232244a`)
- The model server CLI warns when a model is launched with a context window too
  small for agent work. (2026-09-17, `fc05205`)
- The model server CLI shows whether streaming is enabled per request on a model
  call. (2026-09-21, `6cc0ddc`)

### Known issues

Open and documented; see the milestone board for the full picture.

- LLM backend switching is env surgery rather than a UI control — the selector UX
  is the blocker. (#21)
- Langfuse shows one trace per model call instead of one per turn, because no
  session id reaches the agent invocation. (#22)
- The OpenRouter backend emits no per-request HTTP log, unlike the local backend.
  (#24)
- Live token streaming is not implemented; the `--streaming` flag is inert and the
  answer arrives whole. (#17, #19)
- Interacting with the UI mid-stream can drop the in-flight turn. (#20)
- Rotating the model-server key requires a manual `.env` edit and an A2A restart. (#13)

## [0.1.0]

Initial working agent (2026-06-03 → 2026-08-27).

### Added

- **Agent core** — a LangChain agent built on `create_agent`, later refactored into
  the `AgentRuntime` class so heavy initialisation happens at startup instead of at
  import time. (2026-08-22, `0a211b2`)
- **External system prompt** — the report agent's instructions live in
  `agent/reports.md` rather than in code. (2026-06-03, `998a668`)
- **Virtual filesystem** — a `/context/` area persisted in the store and shared
  across sessions, plus an ephemeral `/scratchpad/` for working notes, wired through
  `FilesystemMiddleware` from `deepagents`. (2026-07-06, `d0bd839`)
- **Context library** — recon and enumeration interpretation guides, and a recon
  report template. (2026-06-03 → 2026-06-15)
- **`export_report_to_local_disk` tool** — extracts a finished report from the
  virtual filesystem onto real disk. (2026-06-15)
- **A2A server** — a Starlette app exposing the agent over JSON-RPC, with streaming
  responses and immediate task acknowledgement. (2026-07-06, `98cbc6e`)
- **Sequential execution lock** — concurrent A2A clients are serialised so
  provider rate limits cannot be overrun. (2026-07-15, `788447c`)
- **Langfuse observability** — tracing wired in, degrading gracefully when
  unreachable. (2026-07-01, `8b55298`)
- **Middleware stack** — rate limiting, and retry with exponential backoff and
  jitter. (2026-07-06)
- **Postgres session persistence** — LangGraph checkpoints and store, with TCP
  keepalives so remote connections survive firewall timeouts. (2026-06-03 →
  2026-07-07)
- **Local SQLite fallback** — the whole persistence layer runs offline from a single
  file when Postgres is unavailable. (2026-08-27, `5e46b46`)
- **Store CLI** — seed, inspect, wipe, export, and delete against the virtual
  filesystem. (2026-08-27, `5e46b46`)
- **Project packaging** — `pyproject.toml` with pinned dependencies and pytest path
  configuration. (2026-07-16, `71a3d01`)
- **Two test suites** — acceptance tests against the real runtime, and mock tests
  that isolate layers so failures are debuggable in minutes rather than an hour.
- **Docs** — project overview and an initial architecture sketch.

### Changed

- Debugging was simplified: the agent's construction moved behind a single runtime
  class with clean imports. (2026-08-22, `0a211b2`)
- The development cycle was restructured around the two-layer test split.
  (2026-07-09, `c830732`)
- A2A concurrent-connection handling was simplified. (2026-07-17, `caf26ff`)
- Database access moved to a Tailscale-reachable host for remote work. (2026-07-21)