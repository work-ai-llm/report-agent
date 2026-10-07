# Troubleshooting

Real incidents, and how to tell what is happening. Each of these cost real
debugging time; they are ordered by how often they come up.

Start with the connectivity banner before anything else:

```bash
python -m streamlit_ui --agent real --backend sqlite --debug
```

It probes the real components and reports which backend answered, so it settles
"which one is even running" immediately. If a line says `not probed`, that
component was never checked — the banner does not guess.

---

## The agent answers as if it had an error

**Symptom.** The chat returns a message that reads like the model reporting a
failure, rather than raising anything.

**Cause.** `CredentialRefreshRetryMiddleware` is configured with
`on_failure='error'`, and that is the only value that re-raises. Any other value —
including the `'raise'` this project used to have — formats the failure into a
`ModelResponse` holding an `AIMessage`, which reaches the agent as ordinary
model output.

If you change `on_failure`, read `ModelRetryMiddleware._handle_failure` first.

---

## Everything reports zero tokens

**Symptom.** Langfuse shows `promptTokens 0` and an empty `usageDetails` on every
generation. `usageMetadata` on the assistant messages is `null`.

**Cause.** The model server never sent a usage block. This is not a Langfuse
problem — Langfuse can only report what the provider returns.

Streaming endpoints only emit usage when asked, and `langchain-openai` only sets
`stream_options: {"include_usage": true}` automatically for the *default* OpenAI
base URL. This project always passes an explicit `base_url`, so the flag stays
off.

Check that the `ChatOpenAI` branch of `_init_llm` still passes
`stream_usage=True`. Note this only affects `openai_local`; the OpenRouter and
Mistral branches do not request usage, so their counts stay absent by design.

`tests/experiments/exp_token_usage.py` proves the behaviour on the wire, with a
`stream_usage=False` control.

---

## A rotated API key kills every request

**Symptom.** After the model server restarts, every call returns 401 until the
key is re-synced **and** the process is restarted.

**Should not happen.** The credential is re-read from the `.env` file on an auth
failure and the request is retried in place. Editing `.env` under a running server
is enough.

If you hit this, the refresh is not firing. Check that:

- the key is in the `.env` **file**. The refresh reads the file, not
  `os.environ`, because the environment still holds the value captured at import.
  A key set only in the shell will not be picked up.
- the backend is `openai_local` or `openrouter`. Mistral keeps its key inside its
  own client object with no equivalent handle, so a Mistral 401 follows the normal
  retry path and fails.
- the client being patched is the one being called. `ChatOpenAI` holds a separate
  `root_client` and `root_async_client`, and this agent is async end to end.
  Updating only the sync one looks correct in a test and changes nothing at
  runtime.

`tests/experiments/exp_credential_refresh_live.py` exercises the whole path
against a live endpoint, using a temporary `.env` so your own is untouched.

---

## Pinning a model-server key does not work

**Symptom.** `model_server_cli.py launch --secret <value>` appears to accept the
value, but the server issues a different secret.

**Cause.** The manager mints its own secret on every launch. The CLI's
`--secret` is never transmitted to the manager — it only overrides what gets
written to `.model_server_state.json` locally.

That override is a hazard rather than a convenience: if it differs from the real
secret, the saved state is poisoned and `stop`, `endpoint` and `chat` will fail
against it. The launch response is the only trustworthy source of the current
secret.

---

## Langfuse shows one trace per model call

**Should not happen.** Both surfaces pass `metadata["langfuse_session_id"]`, which
the `CallbackHandler` reads at the chain root and propagates to every child
observation.

If traces are not grouping, check the key and its location. It must be
`config["metadata"]["langfuse_session_id"]`. Two adjacent keys look plausible and
do **not** work:

- `configurable.session_id` — not read by the handler at all
- `metadata["session.id"]` — not a recognised key; silently ignored

Each turn is one trace, and all turns of a thread share one session. That is
correct: trace boundaries are fixed by LangChain and cannot span two invocations.

---

## The agent ignores which backend it should be using

**Symptom.** `LLM_BACKEND=openrouter` but the run behaves like the local server,
or a stale model id causes a 404 from the provider.

**Check the banner first.** `--debug` prints the resolved backend and model.

The model-id variables are backend-agnostic by design, so a value left over from
another backend survives the switch. A leftover that cannot belong to the
selected backend is reported at startup and the default is used — check the
startup output for that warning before suspecting the provider.

If the warning did not fire, the mismatch is one the heuristic cannot see: it only
checks OpenRouter for a missing `/`. For other backends an arbitrary id is
accepted verbatim and fails at the provider.

---

## No HTTP request logs from the OpenRouter backend

**Should not happen.** The log configuration names both `httpx` and `httpx2`,
because the backends use different HTTP libraries. Naming only one leaves the
other looking mute.

`LOG_LEVEL` tiers are `NONE`, `INFO`, `FULL`, `DEBUG` — default `NONE`. There is
no `WARNING` tier. At `INFO` you get one line per LLM HTTP request; at `FULL` and
above, every logger in the process reports.

---

## Stopping the UI does not stop the agent

**Symptom.** Clicking Stop, or interrupting the process, leaves the agent running.
Langfuse warnings about scheduling futures after shutdown appear at exit.

**Cause.** `RealAgentRunnerAdapter.run` discards the `Future` returned by
`schedule()`, and the consumer blocks on `events.get()` with no timeout.
Abandoning the generator — which is what Stop does — never cancels the work on
the event loop. It runs to completion.

Tracked in #39. The shutdown warnings are a symptom of the same thing, not a
separate fault.

---

## The A2A server is unreachable from another machine

**Check which bind it is on.** The default is loopback, so this is expected until
you ask otherwise.

`--host all_interfaces` widens it to every NIC. A specific address can be pinned
with `A2A_BIND`, at the cost of not surviving a Wi-Fi IP change.

`A2A_HOST` is not a bind control — it only changes the IP advertised in the agent
card. If the bind and the card disagree the server looks healthy: the card fetch
succeeds and every subsequent call is refused, because the client sends calls to
the URL in the card.

There is no authentication. Anything reachable on the network can drive the agent.

---

## An A2A command prints a Python traceback

**Should not happen.** Protocol errors print as one
`[server] <ErrorType>: <message>` line, and Ctrl-C prints a single disconnect
line. Exit codes: `0` ok, `1` protocol error, `2` bad usage, `130` Ctrl-C.

If you see a traceback, the error was not one of those — it is a real bug, and it
was deliberately not swallowed. A non-protocol exception still propagates.

---

## Database connection lost mid-run (A2A)

**Symptom.** A streamed task emits `Database connection lost, healing and
resuming...` and then may still fail.

**Cause and limit.** `_consume_agent_stream` heals a dropped Postgres pool once
and resumes the **same** thread. That works when the pool is stale. It does not
help if the database is genuinely down, in which case the second attempt raises.

---

## Adding a dependency does not install cleanly

Check whether it was added to `pyproject.toml` at all, and whether it is imported
before being pinned. `docs/dependencies.md` lists the known gaps in this area,
including one real one: `aiosqlite` is imported by production code and is not
declared.

---

## Update this when

- a new failure mode is diagnosed
- a section here is found to be wrong or incomplete
- a fix lands that makes a "should not happen" section obsolete — delete it then,
  rather than leaving it to confuse the next reader

Not for: anything already covered by `.env.example` (variables and defaults) or
`AGENTS.md` (traps and invariants).