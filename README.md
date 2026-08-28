# Report Agent

An AI-powered penetration testing report generation agent built with LangChain, LangGraph, and Mistral AI.

## Overview

This project implements an intelligent agent that automates the creation of penetration testing reports. It leverages:

- **Mistral AI** (mistral-large-2512) for natural language understanding and report generation
- **LangChain/LangGraph** for agent orchestration and tool management
- **PostgreSQL** (with automatic local SQLite fallback) for persistent storage of conversation history and virtual filesystem
- **Langfuse** for observability and tracing
- **deepagents** for virtual filesystem middleware
- **A2A Server** for agent-to-agent communication
- **Asyncio Lock** for concurrent agent connection restriction

## Features

- **Virtual Filesystem**: Persistent storage for context files (templates, interpretation guides) and session-specific scratchpad files
- **Tool Emulation**: Mock tools for testing report export functionality
- **Rate Limiting**: Built-in rate limiter to respect API quotas (1 request per 20 seconds)
- **Retry Middleware**: Automatic retry on API failures with exponential backoff
- **Session Persistence**: PostgreSQL/SQLite checkpointing for multi-turn conversations
- **Observability**: Full tracing integration with Langfuse

## Installation

### Prerequisites

- Python 3.11 or higher
- Mistral AI API key
- (Optional) PostgreSQL database — if unset or unreachable, the agent falls back to a local SQLite database
- (Optional) Langfuse instance for observability

### Setup

1. **Clone the repository** (if applicable):
   ```bash
   git clone <repository-url>
   cd <project-directory>
   ```

2. **Install dependencies**:
   ```bash
   pip install -e .
   ```

3. **Configure environment variables**:
   
   Copy `.env.example` to `.env` and fill in your credentials:
   ```bash
   cp .env.example .env
   ```

4. **Set up the database**:
   
   Ensure PostgreSQL is running and the database exists. If `POSTGRES_URI` is unset or unreachable, the agent falls back to a local SQLite database at `agent/agent_session.db`. Tables are created automatically on first run.

5. **Seed the virtual filesystem** (optional):
   
   To populate the virtual filesystem with context files:
   ```bash
   python dev_tools/store_cli.py seed --dir agent/context
   ```

## Configuration

### Environment Variables

| Variable | Description | Required |
|----------|-------------|----------|
| `MISTRAL_API_KEY` | Your Mistral AI API key | Yes |
| `POSTGRES_URI` | PostgreSQL connection string (fallback: local SQLite if unset/unreachable) | No |
| `LANGFUSE_SECRET_KEY` | Langfuse secret key | No |
| `LANGFUSE_PUBLIC_KEY` | Langfuse public key | No |
| `LANGFUSE_BASE_URL` | Langfuse server URL | No (defaults to cloud) |
| `LOG_LEVEL` | Logging verbosity: `INFO`, `DEBUG`, `WARNING` | No (defaults to `INFO`) |

### Rate Limits

The agent is configured with conservative rate limits:
- **1 request per 20 seconds** to Mistral API
- Maximum bucket size: 1 (no bursting)

Adjust in `agent/report_agent.py` if your API key has different limits.

## Usage

WIP

## License

[Add your license here]

## Contributing

[Add contribution guidelines here]
