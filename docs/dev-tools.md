# Dev tools

Five operator CLIs. Three are maintained; two are legacy and should not be
started with.

| Tool | Use it for |
|---|---|
| [`store_cli.py`](#store_cliq) | The virtual filesystem and session store |
| [`model_server_cli.py`](#model_server_cliq) | The local model server |
| [`a2a_cli.py`](#a2a_cliq) | Driving the A2A server by hand |
| `api_connection_test.py` | **Legacy.** Mistral-only connectivity probe, self-described as "no further use" |
| `prep.py`, `2_check_postgr_quick_test.py` | **Legacy.** Superseded by `store_cli.py`; both hardcode a thread id |

All of them run from the repository root, and all of them reach the same
singleton the agent does — so they see the same database the agent does.

---

## `store_cli.py`

The virtual filesystem (`("filesystem",)` namespace) and the session store
(checkpointer). It calls `AgentRuntime._init_pool`, which is what makes it share
the agent's database and PRAGMAs rather than configuring its own.

```
python dev_tools/store_cli.py [--sqlite] <command>
```

`--sqlite` forces the local SQLite backend even when `POSTGRES_URI` is set.

| Command | Does |
|---|---|
| `seed --dir DIR` | Copies every file under `DIR` into the virtual filesystem |
| `wipe` | Empties the filesystem namespace |
| `inspect` | Lists what is in it |
| `list-sessions` | Every `thread_id` in the checkpointer |
| `inspect-session --thread-id ID` | Full conversation history for one session |
| `export --key K --out F` | Writes a store entry to a local file |
| `delete --key K` | Removes one entry |
| `reconcile-files` | Normalises keys to the agent's slash-prefixed form |

### Seeding is a required step

`agent/context/` holds the report templates and finding-interpretation guides,
and **they are not loaded automatically**. Nothing in the agent reads that
directory from disk at runtime — the agent only ever sees the store.

```bash
python dev_tools/store_cli.py seed --dir agent/context
```

Without it the agent finds `/context/templates/` and `/context/interpretations/`
empty and has nothing to follow. This is the single most likely reason a fresh
checkout produces a worse report than expected.

Keys land slash-prefixed and relative to `/context/` — seeding `agent/context`
produces `/templates/recon_report_template.md`, which is exactly what the agent
asks for when it reads `/context/templates/...`. The two are not in conflict; the
`/context/` route strips its prefix before hitting the store.

Findings are **not** seeded. They arrive through the UI uploader, which writes to
`/findings/`.

### Key format

Every key is slash-prefixed: `/findings/nmap.txt`, not `findings/nmap.txt`. The
UI used to strip the slash, which made uploads invisible to the agent and
agent-written reports listed-but-unreadable. If you write to the store yourself,
match the slash form or run `reconcile-files` afterwards.

---

## `model_server_cli.py`

Client for the remote llama.cpp model manager that serves the `openai_local`
backend.

```
python dev_tools/model_server_cli.py <status|launch|stop|resources|estimate|endpoint|chat>
```

| Command | Does |
|---|---|
| `status` | What is running, what is available, idle countdown |
| `launch --model M` | Starts a model |
| `stop --model M` | Stops it |
| `resources` | Live RAM/VRAM and the max context per KV dtype |
| `estimate --model M` | Precomputed speeds and the recommended offload |
| `endpoint [--env]` | Prints the OpenAI-compatible base URL and secret |
| `chat` | Smoke-tests the `/v1` proxy |

`endpoint --env` prints ready-to-paste PowerShell assignments. The current secret
is only trustworthy from the **launch response** — saved state can lag.

### Two traps

**`launch --secret` does not pin the key.** The manager mints its own secret on
every launch, and `--secret` is never transmitted to it. The flag only overrides
what gets written to `.model_server_state.json` locally, so passing a value that
differs from the real one poisons that state and breaks `stop`, `endpoint` and
`chat`. Read the secret from the launch response.

**`ctx` matters.** A context window below ~50,000 is flagged as below advisory,
because tool output and file dumps routinely exceed it and model calls start
failing in a way that looks like a model problem.

---

## `a2a_cli.py`

One subcommand per `A2AClient` method.

```
python dev_tools/a2a_cli.py [--server URL] <list|send|get|cancel|subscribe|poll>
```

`--server`, else `A2A_SERVER_URL`, else `http://127.0.0.1:9999`. Note `--server`
is a **top-level** flag and must precede the subcommand.

`subscribe` only works on a live task; for a finished one use `get`.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Success |
| `1` | A protocol error from the server — printed as one `[server] <ErrorType>: <message>` line, plus a hint where one applies |
| `2` | Bad usage from argparse |
| `130` | Ctrl-C. The client disconnected; **the server keeps working** |

Only protocol errors are caught. Anything else propagates as a traceback, because
a real bug should not be dressed up as a server message.

---

## Update this when

- a tool gains or loses a subcommand, flag, or env var
- a trap above changes, or a new one is found
- a legacy tool is deleted or promoted to maintained
- `store_cli.py` changes how keys are formed

Not for: the agent's own behaviour (that is `architecture.md`) or env var
definitions (that is `.env.example`).