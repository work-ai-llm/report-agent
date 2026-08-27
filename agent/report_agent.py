import asyncio
import sys
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import os
import logging
import httpx
import aiosqlite
from pathlib import Path
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool, ToolRuntime
#from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from psycopg_pool import ConnectionPool, AsyncConnectionPool
from psycopg.rows import dict_row


# NEW IMPORTS for Persistent Virtual Filesystem
from langgraph.store.postgres import PostgresStore, AsyncPostgresStore
from langgraph.store.sqlite.aio import AsyncSqliteStore
from deepagents.middleware import FilesystemMiddleware
from deepagents.backends import CompositeBackend, StateBackend, StoreBackend
from langchain_core.rate_limiters import InMemoryRateLimiter
from langchain.agents.middleware import LLMToolEmulator, ModelRetryMiddleware
from langchain_mistralai import ChatMistralAI # Required to attach rate limiter to the model

# --- NEW: LANGFUSE INITIALIZATION ---
from langfuse.langchain import CallbackHandler
from langfuse import get_client

load_dotenv()


# ==========================================
# Logging setup (global process state, stays at module level)
# ==========================================
# Selectable via env: LOG_LEVEL=DEBUG for httpx/langchain, else INFO (default)
# Best practice (vs hard-coded DEBUG): use env var so import doesn't force verbose
# Professional codebases use logging.getLogger(__name__) + dictConfig/env, not basicConfig(DEBUG) at import
LOG_LEVEL_STR = os.getenv("LOG_LEVEL", "INFO").upper()
LOG_LEVEL = getattr(logging, LOG_LEVEL_STR, logging.INFO)
logging.basicConfig(level=LOG_LEVEL)
# Only enable verbose for httpx/httpcore/langchain when explicitly DEBUG
if LOG_LEVEL == logging.DEBUG:
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.DEBUG)
    logging.getLogger("langchain").setLevel(logging.DEBUG)
else:
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("langchain").setLevel(logging.WARNING)
# aiosqlite DEBUG is noisy (every PRAGMA/SELECT) - keep at INFO unless LOG_LEVEL==DEBUG
if LOG_LEVEL != logging.DEBUG:
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)


# ==========================================
# Module-level constants (safe, no side effects)
# ==========================================

DB_URI = os.getenv("POSTGRES_URI")

CONTEXT_DIR = Path(__file__).parent / "context"

sys_prompt_path = Path(__file__).parent / "reports.md"

# Local SQLite fallback (single file, two tables inside - same pattern as Postgres)
LOCAL_DB_PATH = Path(__file__).parent / "agent_session.db"


# ==========================================
# Tool definition (just a function, no execution at import)
# ==========================================

@tool
def export_report_to_local_disk(
    session_id: str,
    report_filename: str,
    runtime: ToolRuntime
) -> str:
    """
    Extracts the final compiled report from the persistent virtual filesystem 
    (Postgres Store) and saves it to a real local folder for external access.
    
    Args:
        session_id: The ID of the pentest session.
        report_filename: The name of the file as it was saved in the virtual FS (e.g., 'final_recon_report.md').
    """
    assert runtime.store is not None, "Agent runtime store is not available."
    
    # 1. Define where the FilesystemMiddleware saved it
    # If the agent wrote to "/context/reports/final_recon_report.md"
    namespace = ("filesystem",)
    key = f"reports/{report_filename}" 
    
    # 2. Retrieve the JSON document from the Postgres Store
    item = runtime.store.get(namespace, key)
    if not item:
        return f"ERROR: Could not find report '{key}' in namespace {namespace}."
        
    # FilesystemMiddleware stores the file content. We extract it safely.
    content = item.value
    if isinstance(content, dict):
        content = content.get("content") or content.get("text") or str(content)
        
    # 3. Write to a REAL local disk folder
    output_dir = Path(__file__).parent / "exported_reports"
    output_dir.mkdir(exist_ok=True) # Create the folder if it doesn't exist
    
    final_path = output_dir / f"{session_id}_{report_filename}"
    final_path.write_text(str(content), encoding="utf-8")
    
    return f"Successfully exported report to local disk: {final_path.absolute()}"


# ==========================================
# AgentRuntime class
# ==========================================

class AgentRuntime:
    """
    Manages the lifecycle of the report agent and its dependencies.
    
    All heavy initialization (network calls, DB connections, LLM setup)
    happens in initialize(), not at import time. This makes the module
    safe to import for testing without triggering side effects.
    """
    
    def __init__(self):
        self.pool = None
        self.checkpointer = None
        self.store = None
        self.agent = None
        self.langfuse = None
        self.langfuse_handler = None
        self.system_prompt_text = None
        self.fs_middleware = None
        self.llm = None
        self.tool_emulator = None
        self.retry_middleware = None
    
    # ==========================================
    # Public API
    # ==========================================
    
    async def initialize(self):
        """
        Full initialization — called from Starlette lifespan.
        
        Delegates to private methods for each layer.
        If one layer fails, you know exactly which layer it is.
        """
        self._init_langfuse()
        self._init_system_prompt()
        self._init_middleware_and_llm()
        await self._init_pool()
        await self._create_agent()
    
    # ==========================================
    # Private initialization methods
    # ==========================================
    
    def _init_langfuse(self):
        """Initialize Langfuse observability client (optional, fails gracefully offline)."""
        try:
            self.langfuse = get_client()
            # auth_check does network I/O - guard with try so offline doesn't crash initialize()
            try:
                if self.langfuse.auth_check():
                    print("Langfuse client is authenticated and ready!")
                else:
                    print("Authentication failed. Please check your credentials and host.")
            except Exception as e:
                print(f"Langfuse auth_check failed ({e}), running without observability")
                self.langfuse_handler = None
                return
            self.langfuse_handler = CallbackHandler()
        except Exception as e:
            print(f"Langfuse init failed ({e}), running without observability")
            self.langfuse = None
            self.langfuse_handler = None
    
    def _init_system_prompt(self):
        """Load the system prompt from disk."""
        if not sys_prompt_path.exists():
            raise FileNotFoundError(f"Could not find prompt file at {sys_prompt_path}")
        
        # Read the entire contents of the file into a string
        self.system_prompt_text = sys_prompt_path.read_text(encoding="utf-8")
        print(f"Loaded system prompt ({len(self.system_prompt_text)} characters)")
    
    def _init_middleware_and_llm(self):
        """Configure middleware, rate limiter, and LLM models."""
        
        # Configure the Filesystem Middleware
        # - /context/ is saved to Postgres (persistent, shared across agents)
        # - /scratchpad/ stays in memory (ephemeral, for agent's temporary notes)
        self.fs_middleware = FilesystemMiddleware(
            backend=CompositeBackend(
                default=StateBackend(),
                # FIX: Explicitly define the namespace to resolve the deprecation warning
                routes={"/context/": StoreBackend(namespace=lambda ctx: ("filesystem",), file_format="v2")}
                ),
            system_prompt=(
                "You have access to a persistent virtual filesystem. "
                "Use 'ls' to explore directories like '/context/templates/' or '/context/interpretations/' "
                "to discover available phases. Use 'read_file' to retrieve the exact content you need."
            ),
            custom_tool_descriptions={
                "ls": "List files and directories. Use this to discover what phases (e.g., recon, enum, exploitation) are available in '/context/'.",
                "read_file": "Read the full contents of a file. Use this to get templates or interpretation guides.",
                "write_file": "Write a new file. Useful for saving drafts in '/scratchpad/draft.md' before finalizing.",
            }
        )
        
        # Rate limiter and LLM setup
        # Enforce a maximum of 1 request per 60 seconds
        rate_limiter = InMemoryRateLimiter(
            requests_per_second=0.05,  # 1 request every 20 seconds
            check_every_n_seconds=0.5,   # Check twice a second if we are allowed to proceed
            max_bucket_size=1,           # No bursting; strict 1-at-a-time limit
        )
        
        # CREATE A RATE-LIMITED SMALL MODEL FOR THE EMULATOR, so it doesn't bypass the queue!
        llm_small = ChatMistralAI(
            model="mistral-small-latest",
            temperature=0.2,
            rate_limiter=rate_limiter, 
            streaming=False,
            timeout=60.0,
        )
        
        # LLM Tool Emulator (Intercepts ONLY these two tools for testing)
        self.tool_emulator = LLMToolEmulator(
            tools=["export_report_to_local_disk"],
            model=llm_small # Use a fast/cheap model to generate the mock responses
        )
        
        # main agent model
        self.llm = ChatMistralAI(
            model="mistral-large-2512",
            temperature=0.2,
            rate_limiter=rate_limiter,
            streaming=True,
            timeout=180.0,
            max_retries=3,
        )
        
        # model request retry middleware, catches api exception and repeates the requests with intervals
        # the timer should be small, otherwise this stuck with rate_limiter and everything breaks
        self.retry_middleware = ModelRetryMiddleware(
            max_retries=3,
            retry_on=(Exception,), # Catch standard API errors
            on_failure='raise',
            backoff_factor=2.0,  
            initial_delay=2.0, # Wait 5s, then 10s, then 15 before retrying
            max_delay=10.0, # Will wait up to 60 seconds between retries if the API is struggling
            jitter=True
        )
    
    async def _init_pool(self):
        """Initialize database pool, checkpointer, and store.
        
        Tries remote Postgres first (if POSTGRES_URI is set and reachable).
        Falls back to local SQLite (single file, two tables) for offline/dev.
        """
        # Legacy code: sync pool implementation (just a reminder for devs)
        # """self.pool = ConnectionPool(
        #     conninfo=DB_URI,
        #     kwargs={
        #         "autocommit": True,       # Required by LangGraph
        #         "prepare_threshold": 0,   # Recommended for connection pools
        #         "row_factory": dict_row,  # Required by LangGraph to read rows as dictionaries
        #     },
        # )
        # self.checkpointer = PostgresSaver(self.pool)
        # self.checkpointer.setup()
        # self.store = PostgresStore(self.pool)
        # self.store.setup() # for async it shouldn't be called
        # """   
        
        # --- Try Postgres if DB_URI is configured ---
        if DB_URI:
            try:
                # Append connect_timeout so psycopg fails fast (not 30s) when offline
                conninfo = DB_URI
                if "connect_timeout" not in conninfo:
                    sep = "&" if "?" in conninfo else "?"
                    conninfo = f"{conninfo}{sep}connect_timeout=3"
                self.pool = AsyncConnectionPool(
                    conninfo=conninfo,
                    kwargs={
                        "autocommit": True,
                        "prepare_threshold": 0,
                        "row_factory": dict_row,
                        "keepalives": 1,
                        "keepalives_idle": 30,
                        "keepalives_interval": 10,
                        "keepalives_count": 3,
                    },
                    open=False,
                    timeout=3.0,
                )
                # Short timeout so local fallback is fast when offline
                await asyncio.wait_for(self.pool.open(), timeout=4.0)
                
                self.checkpointer = AsyncPostgresSaver(self.pool)
                await self.checkpointer.setup()
                
                self.store = AsyncPostgresStore(self.pool)
                await self.store.setup()
                print(f"Using Postgres backend: {DB_URI[:30]}...")
                return
            except Exception as e:
                print(f"Postgres unreachable ({e}), falling back to local SQLite: {LOCAL_DB_PATH}")
                if self.pool is not None:
                    try:
                        await self.pool.close()
                    except Exception:
                        pass
                    self.pool = None
        
        # --- Fallback: local SQLite (same file, two tables - checkpoints + store) ---
        # This mirrors Postgres pattern: one server/file, separate tables for saver vs store.
        # Reuses same self.checkpointer / self.store variables, just different backend.
        self.pool = None  # No pg pool in local mode
        LOCAL_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        
        # Two connections to same file - mirrors pool's ability to serve both saver+store
        # (see explanation below why pool can't be used for SQLite)
        # timeout/busy_timeout must match sync CLI (dev_tools/store_cli.py) to avoid 'database is locked'
        # when both async agent and sync CLI share same file (WAL mode)
        conn_checkpointer = await aiosqlite.connect(str(LOCAL_DB_PATH), timeout=30.0, isolation_level=None)
        conn_store = await aiosqlite.connect(str(LOCAL_DB_PATH), timeout=30.0, isolation_level=None)
        await conn_checkpointer.execute("PRAGMA journal_mode=WAL;")
        await conn_checkpointer.execute("PRAGMA busy_timeout=30000;")
        await conn_checkpointer.execute("PRAGMA synchronous=NORMAL;")
        await conn_store.execute("PRAGMA journal_mode=WAL;")
        await conn_store.execute("PRAGMA busy_timeout=30000;")
        await conn_store.execute("PRAGMA synchronous=NORMAL;")
        
        self.checkpointer = AsyncSqliteSaver(conn_checkpointer)
        await self.checkpointer.setup()
        
        self.store = AsyncSqliteStore(conn_store)
        await self.store.setup()
        print(f"Using local SQLite backend: {LOCAL_DB_PATH}")
    
    async def _create_agent(self):
        """Create the LangChain agent with all middleware."""
        self.agent = create_agent(
            model=self.llm,
            system_prompt=self.system_prompt_text,
            tools=[export_report_to_local_disk],
            #checkpointer=InMemorySaver()
            store=self.store,
            middleware=[self.fs_middleware, self.tool_emulator, self.retry_middleware],
            checkpointer=self.checkpointer
        )
        
        callbacks = [self.langfuse_handler] if self.langfuse_handler else []
        # Phoenix alternative (when added): OpenTelemetry instrumentation, not callbacks:
        #   from phoenix.otel import register; register()
        #   from openinference.instrumentation.langchain import LangChainInstrumentor
        #   LangChainInstrumentor().instrument()  -- auto-captures spans, no handler needed
        self.agent = self.agent.with_config({"callbacks": callbacks}) if callbacks else self.agent


# ==========================================
# Module-level instance (lazy initialization)
# ==========================================
# This is the single instance used by a2a_connector and dev_tools.
# Import this, then call runtime.initialize() when ready.
runtime = AgentRuntime()
