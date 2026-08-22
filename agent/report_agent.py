import os
import logging
import httpx
from pathlib import Path
from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.tools import tool, ToolRuntime
#from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg_pool import ConnectionPool, AsyncConnectionPool
from psycopg.rows import dict_row


# NEW IMPORTS for Persistent Virtual Filesystem
from langgraph.store.postgres import PostgresStore, AsyncPostgresStore
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
# 1. FORCE LANGCHAIN TO SHOW REAL ERRORS
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("httpx").setLevel(logging.DEBUG)
logging.getLogger("httpcore").setLevel(logging.DEBUG) 
logging.getLogger("langchain").setLevel(logging.DEBUG)


# ==========================================
# Module-level constants (safe, no side effects)
# ==========================================

DB_URI = os.getenv("POSTGRES_URI")

CONTEXT_DIR = Path(__file__).parent / "context"

sys_prompt_path = Path(__file__).parent / "reports.md"


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
        """Initialize Langfuse observability client."""
        self.langfuse = get_client()
        
        # Verify connection
        if self.langfuse.auth_check():
            print("Langfuse client is authenticated and ready!")
        else:
            print("Authentication failed. Please check your credentials and host.")
        
        # Initialize the handler. It automatically reads LANGFUSE_PUBLIC_KEY, 
        # LANGFUSE_SECRET_KEY, and LANGFUSE_BASE_URL from your .env file.
        self.langfuse_handler = CallbackHandler()
    
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
        """Initialize database pool, checkpointer, and store."""
        
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
        
        # Async implementation
        self.pool = AsyncConnectionPool(
            conninfo=DB_URI,
            kwargs={
                "autocommit": True,       # Required by LangGraph
                "prepare_threshold": 0,   # Recommended for connection pools
                "row_factory": dict_row,  # Required by LangGraph to read rows as dictionaries

                # --- CRITICAL: TCP KEEPALIVES ---
                # Prevents the OS/Firewall from dropping idle connections during long LLM generations
                "keepalives": 1,
                "keepalives_idle": 30,       # Send heartbeat after 30s of idle time
                "keepalives_interval": 10,   # Resend every 10s if no ack
                "keepalives_count": 3,       # Drop connection after 3 failed heartbeats
                },
            open=False 
        )
        await self.pool.open()
        
        self.checkpointer = AsyncPostgresSaver(self.pool)
        await self.checkpointer.setup()
        
        self.store = AsyncPostgresStore(self.pool)
        await self.store.setup()
    
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
        
        self.agent = self.agent.with_config({"callbacks": [self.langfuse_handler]})


# ==========================================
# Module-level instance (lazy initialization)
# ==========================================
# This is the single instance used by a2a_connector and dev_tools.
# Import this, then call runtime.initialize() when ready.
runtime = AgentRuntime()
