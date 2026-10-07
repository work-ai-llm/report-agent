# Dependencies

`pyproject.toml` pins every dependency at an exact version. That policy exists
because this project has been broken by transitive drift more than once, and a
floating resolver is what caused it.

This document explains **what each one is for**. The list of versions lives in
`pyproject.toml` and is not restated here.

## Directly used by production code

| Package | Used for |
|---|---|
| `langchain` | `create_agent`, `tool`, middleware types |
| `langchain-core` | Message types, `InMemoryRateLimiter`, retry helpers |
| `langgraph` | Graph runtime under `create_agent` |
| `langchain-openai` | The `openai_local` backend |
| `langchain-openrouter` | The `openrouter` backend |
| `langchain-mistralai` | The `mistral` backend |
| `deepagents` | `FilesystemMiddleware` and the store/state backends behind the virtual filesystem |
| `langgraph-checkpoint-postgres` | `AsyncPostgresSaver` |
| `langgraph-checkpoint-sqlite` | `AsyncSqliteSaver` for the offline fallback |
| `psycopg` / `psycopg-pool` | Postgres pool, and `OperationalError` which the A2A heal path catches |
| `langfuse` | Tracing. Degrades to a no-op handler when unreachable. |
| `a2a-sdk` | Both the A2A server and the A2A client |
| `starlette` / `uvicorn` | The A2A ASGI app |
| `streamlit` | The whole UI, plus `AppTest` used by the tests |
| `httpx` | A2A client transport, and the push-notification sender |
| `python-dotenv` | `.env` loading at import, and re-reading it on credential refresh |
| `requests` | Only `dev_tools/model_server_cli.py` |
| `protobuf` | Message → dict conversion over A2A protobufs |
| `pytest` / `pytest-asyncio` | The mock suite |

## Pinned but not imported

These are pinned to **freeze the transitive graph**, not because the project
calls them. Do not read them as requirements.

Most of the list is transitive: `langsmith`, `langgraph-checkpoint`,
`langgraph-prebuilt`, `langgraph-sdk`, `tenacity`, `backoff`, `orjson`,
`ormsgpack`, `fsspec`, `filelock`, `wcmatch`, `tokenizers`, `huggingface-hub`,
`pydantic` / `pydantic-core`, `typing-extensions`, `json-rpc`, `jsonpatch`,
`httpx-sse`, `sse-starlette`, `websockets`, and the usual transitive web stack
(`certifi`, `idna`, `urllib3`, `anyio`, `h11`, `httpcore`, `click`, `typer`,
`rich`, `pygments`).

Two deserve a warning, because they look like support we do not have:

- **`langchain-anthropic` and `langchain-google-genai`** are pinned. There is no
  Anthropic or Gemini code path in this repository. They are extras pulled in by
  `deepagents` / `langchain`. They inflate the install and will read as
  "we support five providers" when the project supports three.
- **`httpx2`** is the transport the newer openai SDK uses. It appears only in
  `tests/experiments/exp_token_usage.py`, but it is why `LOG_LEVEL` names both
  `httpx` and `httpx2` when configuring per-request logging.

## Known dependency problems

Three things in this area are wrong. They are listed here rather than silently
left for the next person to trip over.

### `aiosqlite` is used but not declared

`agent/report_agent.py` imports `aiosqlite` for the local SQLite fallback. It is
**not** in `pyproject.toml`. It resolves today only because
`langgraph-checkpoint-sqlite` depends on it transitively.

Against a project that pins everything exactly, that is a real hole: any
upstream change to that package's own dependencies silently breaks the entire
SQLite fallback — the path that is supposed to work when nothing else is
reachable. It should be pinned explicitly.

### Packaging does not include the UI

`[tool.setuptools.packages.find]` includes `agent*`, `client*`, `dev_tools*` and
`tests*` — but not `streamlit_ui`. After an editable install, `import
streamlit_ui` works only from the repository root.

Related: `client/` and `dev_tools/` have no `__init__.py` and are matched by the
non-namespace finder, so they are not packaged either. `dev_tools/a2a_cli.py`
imports `client.a2a_client` with no `sys.path` bootstrap, unlike its siblings.

Everything currently works because the tools are run from the repo root. That is
an accident of how they are invoked, not a guarantee.

### `tests` is packaged, and production imports it

`app.py`, `runtime_status.py` and `real_backend.py` import the mock objects from
`tests/mock/`. So an editable install installs a top-level `tests` package, which
will shadow any other `tests` package on the machine.

This is unusual and deliberate — it is what lets the UI run in mock mode with no
services — but it is worth knowing before installing into a shared environment.

---

## Update this when

- a dependency is added, removed, or its role changes
- a pin moves from transitive to direct, or the reverse
- a new finding like the ones above appears
- backend support is added or removed

Not for: version bumps with no change in purpose.