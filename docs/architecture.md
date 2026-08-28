# Architecture

## Components

1. **Agent Core** (`report_agent.py`)
   - Creates the LangChain agent with Mistral AI
   - Configures middleware (filesystem, tool emulator, retry)
   - Manages database connections (PostgreSQL with local SQLite fallback)

2. **Virtual Filesystem**
   - `/context/` - Persistent, shared across sessions (PostgreSQL-backed)
   - `/scratchpad/` - Ephemeral, session-specific (in-memory)

3. **Tools**
   - `export_report_to_local_disk` - Exports reports from virtual FS to local disk
   - Filesystem tools (ls, read_file, write_file) via middleware

4. **Middleware Stack**
   - `FilesystemMiddleware` - Virtual filesystem operations
   - `LLMToolEmulator` - Mocks specific tools for testing
   - `ModelRetryMiddleware` - Handles API retries

5. **A2A Server**
   - Enables agent-to-agent communication
   - Facilitates distributed task execution

6. **Asyncio Lock**
   - Provides concurrent agent connection restriction
   - Ensures thread-safe access to shared resources

## Data Flow

```
User Input → LangChain Agent → Mistral AI → Tool Execution → PostgreSQL
                                    ↓
                              Langfuse (tracing)
```

## System Design

### Agent Orchestration

The agent is built using LangGraph for stateful orchestration. It manages:
- Conversation state through PostgreSQL checkpoints
- Tool execution through the middleware stack
- Rate limiting to respect API quotas

### Storage Layers

1. **PostgreSQL / SQLite**
   - Conversation history and checkpoints
   - Virtual filesystem context files
   - Session metadata
   - SQLite (`agent/agent_session.db`) is used automatically when Postgres is unavailable

2. **In-Memory**
   - Scratchpad files (session-specific)
   - Temporary computation results

### Observability

Full tracing integration with Langfuse provides:
- Request/response logging
- Latency tracking
- Token usage metrics
- Error tracking
