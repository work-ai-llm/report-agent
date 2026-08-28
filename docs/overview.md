# Project Overview

An AI-powered penetration testing report generation agent built with LangChain, LangGraph, and Mistral AI. This project automates the creation of security assessment reports through intelligent agent orchestration.

## Key Features

- **Virtual Filesystem**: Persistent storage for context files (templates, interpretation guides) and session-specific scratchpad files
- **Tool Emulation**: Mock tools for testing report export functionality
- **Rate Limiting**: Built-in rate limiter to respect API quotas (1 request per 20 seconds)
- **Retry Middleware**: Automatic retry on API failures with exponential backoff
- **Session Persistence**: PostgreSQL/SQLite checkpointing for multi-turn conversations
- **Observability**: Full tracing integration with Langfuse
- **A2A Server**: Agent-to-agent communication capabilities
- **Concurrent Agent Connection Restriction**: Via asyncio lock for thread-safe operations

## Tech Stack

### Core Technologies
- **Python 3.11+**: Primary programming language
- **LangChain/LangGraph**: Agent orchestration framework
- **Mistral AI** (mistral-large-2512): LLM provider for natural language understanding and report generation
- **PostgreSQL / SQLite**: Database for persistent storage (PostgreSQL with automatic local SQLite fallback)
- **Langfuse**: Observability and tracing platform
- **deepagents**: Virtual filesystem middleware

### Architecture Components

1. **Agent Core** (`agent/report_agent.py`)
   - Creates the LangChain agent with Mistral AI
   - Configures middleware stack
   - Manages database connections

2. **Virtual Filesystem**
   - `/context/` namespace: Persistent, shared across sessions (PostgreSQL-backed)
   - `/scratchpad/` namespace: Ephemeral, session-specific (in-memory)

3. **Tools**
   - `export_report_to_local_disk`: Exports reports from virtual FS to local disk
   - Filesystem tools (ls, read_file, write_file) via middleware

4. **Middleware Stack**
   - `FilesystemMiddleware`: Virtual filesystem operations
   - `LLMToolEmulator`: Mocks specific tools for testing
   - `ModelRetryMiddleware`: Handles API retries

5. **A2A Server**
   - Enables agent-to-agent communication
   - Facilitates distributed task execution

6. **Asyncio Lock**
   - Provides concurrent agent connection restriction
   - Ensures thread-safe access to shared resources

## Development Tools

### Store CLI (`dev_tools/store_cli.py`)
Command-line interface for managing the virtual filesystem:
- Inspect stored files
- Seed from local directories
- Wipe namespaces
- Export/import files
- Inspect session state

### Testing Utilities
- `api_connection_test.py`: Verify Mistral API connectivity
- `2_check_postgr_quick_test.py`: Debug conversation history and inspect threads
- `prep.py`: Store seeding utility

### Test Suites
- `tests/acceptance/`: Agent structure, DB initialization, and A2A connectivity (real runtime)
- `tests/mock/`: A2A concurrency lock and task cancellation tests (fake agent, no external services)

## Limitations

### Rate Limiting
- Currently configured for 1 request per 20 seconds to Mistral API
- Maximum bucket size: 1 (no bursting)
- May need adjustment based on your API key's specific limits

### Database Dependencies
- PostgreSQL is optional — if `POSTGRES_URI` is unset or unreachable, the agent uses a local SQLite backend (`agent/agent_session.db`)
- TCP keepalives enabled by default to prevent firewall timeouts
- Connection string must be properly configured in environment variables

### API Dependencies
- Requires valid Mistral AI API key
- Subject to Mistral API availability and quota limits
- Langfuse integration is optional but recommended for production use

### Virtual Filesystem
- Context files are persistent but require manual seeding
- Scratchpad files are ephemeral and lost on session end
- File operations go through middleware which may add latency

### Concurrency
- Asyncio lock restricts concurrent agent connections
- Designed for single-agent-per-thread scenarios
- May require tuning for high-concurrency deployments

## Getting Started

For installation and usage instructions, see the [README](../README.md).
For detailed architecture information, see [architecture.md](./architecture.md).
For version history, see [changelog.md](./changelog.md).
