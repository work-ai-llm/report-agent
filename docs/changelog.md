# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/) and the
project intends to follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

**On the version labels below:** the repository has no git tags and no GitHub
releases. These blocks are reconstructed from `master` history and mark the two
periods of work, not published releases. The first real release is still to come.
Commit dates and short SHAs are given so any entry can be traced back.

## [Unreleased]

After the initial agent (2026-08-28 → 2026-10-05).

### Added

- **`report-generation` skill** - the report workflow now ships as a skill
  (`agent/skills/report-generation/SKILL.md`) instead of living in the system
  prompt. Only the skill's `name` and `description` are injected; the agent pulls
  the workflow with `read_file` when it decides the skill applies, and says it is
  skipping the skill when it does not. Gated to larger reports by its own
  `description` - the gate is a sentence in the skill, not code.
- **`/skills/` filesystem route** - the virtual filesystem gained a third route,
  to real disk, so editing a `SKILL.md` changes agent behaviour with no code
  change and no restart.
- **Cross-check phase in the skill** - before assembly, every source file's
  distinctive evidence is checked against the drafted sections and gaps are fixed.
- **Skill behaviour experiment** (`tests/experiments/skill_gate_behavior.py`) -
  runs the agent with and without the skill against a known ground truth and
  scores fact retention and invented tokens. Its finding fixtures in
  `results/findings/` are synthetic.
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
  `--llm-backend`, `--debug`, and `--streaming` flags that translate to env vars.
  (2026-10-02; `--llm-backend` 2026-10-05, `fd8279b`)
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
  2026-10-08); `AGENTS.md` as the orientation and trap reference for agents
  (2026-10-03); a reorganised `docs/` with one job per document, a routing table
  from change type to document, and a troubleshooting section seeded from real
  incidents (2026-10-05).
- **Agent filesystem panel** — the sidebar lists everything the agent can read,
  grouped by directory, instead of only uploaded findings and finished reports.
  The context library the agent is told to read is now visible, and display paths
  carry the `/context/` root that the store strips before writing, so the panel
  matches the paths the agent itself says out loud. (#40)
- **Scratchpad panel** — the sidebar shows the agent's working files for the
  current session, which are the drafts it writes while working. The scratchpad
  is not persisted by the agent — it lives in an in-memory backend whose state
  channel is never declared — so the panel reconstructs it by replaying the
  session's own persisted tool calls. It therefore survives a reload, a session
  switch and a restart, and updates live as the run progresses. (#41)

### Security

- **The A2A server no longer binds every network interface by default.** The bind
  is now a two-way `--host` flag: `local` (the default) binds `127.0.0.1`, so the
  port is reachable only from the host, and `all_interfaces` restores the previous
  `0.0.0.0` behaviour, which is also what survives a Wi-Fi IP change. A specific
  address is set with `A2A_BIND` rather than being a third flag value, since
  pinning one interface gives up the IP-change resilience. The agent card's
  advertised URL is derived from the bind, because the client sends every JSON-RPC
  call to the address in the card rather than to the address it was pointed at —
  previously the two could not disagree only because the wildcard bind happened to
  cover whatever was advertised. There is still no authentication on the endpoint
  and every caller shares one task-store owner, so the server says so on startup
  when it is bound widely. (#32, #38)

### Changed

- Storage framing is now Postgres-first with an automatic local SQLite fallback,
  rather than Postgres-only. The README env table was corrected to match.
  (2026-08-28, `05399f6`)
- Acceptance and mock test suites are documented as two deliberate layers, with the
  reasoning recorded in `tests/tests.md`. (2026-08-28, `e7318f0`)

### Fixed

- **Listing findings or reports rewrote matching text anywhere in a filename.**
  The prefix was removed with `str.replace`, so
  `findings/findings_summary.txt` displayed as `summary.txt` — and any later write
  using that displayed name targeted a different key. Only a leading prefix is
  stripped now. (#40)
- **Stopping a Streamlit run did not cancel the in-flight agent stream.** The
  handle from `schedule()` was discarded and the consumer blocked on
  `events.get()` with no cleanup, so pressing Stop — which closes the generator —
  left the agent streaming to completion on the event loop. The handle is now
  cancelled in a `finally`, and a cancelled run deliberately emits no final
  message rather than writing state for a turn the user stopped.
  (2026-10-07, #39)
- **Every Langfuse generation reported zero tokens.** `langchain-openai` only
  auto-enables `stream_usage` for the default OpenAI base URL, and this project
  always passes an explicit `base_url`, so the server was never asked for a usage
  chunk. Now set explicitly on the `ChatOpenAI` branch. (2026-10-05, `82d26ff`)
- **A rotated API key required a manual `.env` edit *and* a process restart.** The
  credential is now re-read from the `.env` file on an auth failure and patched
  onto the live clients, so editing `.env` under a running server is enough. Also
  corrects `on_failure`, which was set to a value that does not re-raise — a dead
  key reached the agent as model-authored text instead of an exception.
  (2026-10-05, `235118a`, #13)
- **The A2A CLI printed Python tracebacks for ordinary outcomes.** A protocol
  refusal is now one `[server] <ErrorType>: <message>` line with a hint naming the
  command to use instead, and Ctrl-C is one disconnect line exiting 130. (2026-10-05,
  `36cd107`, `629e4a7`, #33, #34)
- **The `--debug` banner asserted components were connected without probing
  them**, and named `MISTRAL_API_KEY` regardless of backend. It now reports
  `not probed` when there is nothing to probe, and the real path names the
  resolved backend. (2026-10-05, `2d56539`)
- **Copying `.env.example` verbatim crashed at startup** — `LLM_API_KEY=` is
  present-but-empty, and `os.getenv`'s default only fires when the variable is
  absent. Absent, empty and whitespace-only now all resolve to `"none"`.
  (2026-10-05, `5136e96`)
- **A stale model override from another backend beat the selected backend's
  default**, producing a provider 404 mid-conversation. `LLM_MODEL` is deleted and
  defaults moved to a table; a cross-backend leftover is now reported at startup.
  (2026-10-05, `aa3b886`, #21)
- Bytecode is no longer tracked in git. (2026-09-25, `d2d2046`)
- A duplicated function definition was removed from the agent runtime. (2026-10-03,
  `232244a`)
- The model server CLI warns when a model is launched with a context window too
  small for agent work. (2026-09-17, `fc05205`)
- The model server CLI shows whether streaming is enabled per request on a model
  call. (2026-09-21, `6cc0ddc`)

### Known issues

Open and documented; see the milestone board for the full picture.

- Live token streaming is not implemented; the `--streaming` flag is inert and the
  answer arrives whole. (#17, #19)
- Interacting with the UI mid-stream can drop the in-flight turn. (#20)
- A sidebar selector for the LLM backend is not built. Switching backend is now a
  `--llm-backend` flag rather than an `.env` edit, so this is convenience rather
  than necessity — and the process-wide cache would make a per-user dropdown
  unsafe until it is keyed by backend. (#21)
- The A2A server has no authentication. Loopback-only by default, but
  `--host all_interfaces` exposes an unauthenticated agent to the network.
- `aiosqlite` is imported by production code and was not declared in
  `pyproject.toml`; it resolved transitively. (2026-10-05, now pinned)

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