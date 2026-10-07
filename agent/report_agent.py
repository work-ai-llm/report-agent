import asyncio
import sys
import time
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
import os
import logging
import httpx
import aiosqlite
from pathlib import Path
from dotenv import dotenv_values, find_dotenv, load_dotenv
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
from langchain.agents.middleware._retry import calculate_delay, should_retry_exception
from langchain.agents.middleware import LLMToolEmulator, ModelRetryMiddleware
from langchain_mistralai import ChatMistralAI # Required to attach rate limiter to the model
from langchain_openai import ChatOpenAI   # OpenAI-compatible servers (llama.cpp, model server proxy)
from langchain_openrouter import ChatOpenRouter  # OpenAI-compatible gateway (free-tier models)

# --- NEW: LANGFUSE INITIALIZATION ---
from langfuse.langchain import CallbackHandler
from langfuse import get_client

load_dotenv()


# ==========================================
# Logging setup (global process state, stays at module level)
# ==========================================
# ALLOWLIST at one tier, plain pass-through above it.
#
# `import logging` hands every module in the process the SAME root logger, so
# `logging.getLogger(name)` only ever creates a handle - it selects nothing. A
# logger's effective level is its own if one was set, otherwise the nearest
# ancestor's, otherwise root's (NOTSET means inherit). That is exactly why the
# old denylist leaked: it silenced the three library names it happened to know,
# and when the openai SDK moved from httpx to httpx2 the new logger inherited root
# and started printing. A denylist is a list of today's dependencies; it cannot
# know about tomorrow's.
#
# Four tiers, chosen by LOG_LEVEL at import so that importing this module never
# forces verbosity:
#
#   NONE    - nothing at all. The default: this is a tool you drive, not a server
#             you watch, and silence is the only reliably readable setting.
#   INFO    - one line per LLM HTTP request, plus warnings and errors. Both HTTP
#             client names are listed because the backends use different
#             libraries: the openai SDK logs to `httpx2`, the openrouter SDK to
#             plain `httpx`. Naming only one leaves the other backend looking
#             mute, which is what issue #24 was.
#   FULL    - root at INFO, so every logger in the process reports at INFO. This
#             is the "tell me what the libraries are doing" tier.
#   DEBUG   - root at DEBUG: every logger at DEBUG, including ones nobody named.
#
# This governs `logging` only. The startup, fallback and resolution messages in
# this module are `print()` calls rather than log records, so they still appear
# at NONE.
LOG_LEVEL_STR = os.getenv("LOG_LEVEL", "NONE").upper()
_LOG_TIERS = {"NONE": 0, "INFO": 1, "FULL": 2, "DEBUG": 3}
_TIER = _LOG_TIERS.get(LOG_LEVEL_STR)
if _TIER is None:
    # Fail loud, then fall back to the default. A typo that silently mutes or
    # floods the whole process is the one failure nobody can debug.
    print(
        f"Unknown LOG_LEVEL {LOG_LEVEL_STR!r}; expected one of "
        f"{sorted(_LOG_TIERS)}. Using NONE.",
        flush=True,
    )
    _TIER = _LOG_TIERS["NONE"]

# The floor for the entire process. Anything not named below inherits it, and
# that inheritance is what bounds the output at the two lower tiers.
_ROOT_FLOOR = {
    _LOG_TIERS["NONE"]: logging.CRITICAL + 10,  # above CRITICAL: nothing passes
    _LOG_TIERS["INFO"]: logging.WARNING,
    _LOG_TIERS["FULL"]: logging.INFO,
    _LOG_TIERS["DEBUG"]: logging.DEBUG,
}
logging.basicConfig(level=_ROOT_FLOOR[_TIER])

if _LOG_TIERS["INFO"] <= _TIER < _LOG_TIERS["FULL"]:
    # The single exception to the floor, and the only tier that needs one: at
    # INFO the per-request line is the entire point, and it arrives on one of
    # these two loggers depending on which backend is in use.
    for _name in ("httpx", "httpx2"):
        logging.getLogger(_name).setLevel(logging.INFO)
elif _TIER >= _LOG_TIERS["FULL"]:
    # Release both back to inheritance. A library imported above us may already
    # have pinned one of them - langfuse's own logging setup sets `httpx` to
    # WARNING on import - and if that pin survived, FULL and DEBUG would report
    # the openrouter backend as mute while `httpx2` followed root, which is the
    # same asymmetry issue #24 is about, just inverted. NOTSET hands the
    # decision back to root.
    for _name in ("httpx", "httpx2"):
        logging.getLogger(_name).setLevel(logging.NOTSET)


# ==========================================
# Module-level constants (safe, no side effects)
# ==========================================

# Default model ids per backend, keyed by role.
#
# These are DEFAULTS, not overrides. AGENT_MODEL / AGENT_EMULATOR_MODEL sit on
# top of them, and they are backend-agnostic on purpose: one pair of names for
# all three backends beats three pairs, and a user who switches LLM_BACKEND
# should not have to learn a new variable.
#
# The hazard that follows from "backend-agnostic" is an override left over from
# a different backend - AGENT_MODEL=ornith survives a switch to openrouter,
# where it is not a valid model id, and the failure surfaces as a 404 from the
# provider on the first call rather than at startup. So the id a user actually
# asked for is validated against the selected backend below, and a mismatch is
# reported at startup where it can be read.
#
# There is deliberately no per-backend "fallback default" variable any more.
# LLM_MODEL used to serve that role for openai_local alone, which made a
# backend-specific knob look like a global one.
_BACKEND_MODEL_DEFAULTS = {
    # Local OpenAI-compatible server (llama.cpp and friends).
    "openai_local": {
        "agent": "ornith",
        "emulator": "ornith",
    },
    # OpenRouter free tier. Ids are namespaced "<vendor>/<model>".
    "openrouter": {
        "agent": "inclusionai/ling-3.0-flash-sante:free",
        "emulator": "inclusionai/ling-3.0-flash:free",
    },
    "mistral": {
        "agent": "mistral-medium-latest",
        "emulator": "mistral-small-latest",
    },
}

# Model ids that only exist on one backend. OpenRouter is the only one whose
# ids are namespaced, so it is the only one where the absence of a "/" is a
# reliable signal of a leftover value.
_BACKEND_NAMESPACED_MODEL_IDS = frozenset({"openrouter"})


# --- Credential-refresh retry middleware ---
#
# ModelRetryMiddleware as shipped retries with whatever credential the client
# already holds. For an expired or rotated key that means the same 401 three
# times, separated by ~2s/4s/8s of backoff, before giving up - which is why a
# rotated key reads as a hard outage rather than a transient one (issue #13).
#
# This subclass keeps the parent's loop untouched and only adds one step ahead
# of it: when a failure looks like an auth error, re-read the credential from
# the environment and, if it actually changed, retry immediately. No backoff,
# because the previous attempt carried information rather than failing on its
# own merits.
#
# Auth detection is by class name rather than by importing the openai exception
# types, so this module keeps working for a backend whose SDK is not installed -
# the middleware is constructed unconditionally, while only openai_local and
# openrouter ever produce a matching failure.

_AUTH_ERROR_NAMES = frozenset({
    "AuthenticationError",
    "PermissionDeniedError",
    "UnauthorizedError",
})


def _looks_like_auth_error(exc: BaseException) -> bool:
    """True for a 401/403 from any layer, including wrapped SDK errors."""
    for candidate in _exception_chain(exc):
        if type(candidate).__name__ in _AUTH_ERROR_NAMES:
            return True
        status = getattr(candidate, "status_code", None)
        if status in (401, 403):
            return True
    return False


def _exception_chain(exc: BaseException):
    """Yield ``exc`` and the causes/contexts under it, guarding against cycles."""
    seen = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


class CredentialRefreshRetryMiddleware(ModelRetryMiddleware):
    """``ModelRetryMiddleware`` that re-reads a rotated credential once.

    The retry schedule is the parent's, unchanged: same ``max_retries``, same
    backoff, same ``retry_on`` filtering, same ``_handle_failure`` behaviour.
    The loop is re-implemented here rather than wrapped only because the refresh
    has to happen *inside* an attempt. Wrapping instead - try once, refresh,
    delegate - looks equivalent and is not: the outer call is an extra request
    that the parent's budget does not know about, which changes the attempt
    count for every exception type and skips the first backoff for all of them.

    The one addition: when an attempt fails with an auth error and reloading
    the credential actually changed it, the request is retried immediately, in
    the same attempt slot. No delay, because the previous failure carried
    information rather than failing on its own merits - the key had been
    rotated, not the service flaked.
    """

    def __init__(self, refresh, **kwargs):
        super().__init__(**kwargs)
        self._refresh = refresh

    def _maybe_refresh(self, exc: BaseException) -> bool:
        if not _looks_like_auth_error(exc):
            return False
        try:
            return bool(self._refresh())
        except Exception as refresh_exc:  # pragma: no cover - never mask the error
            print(
                f"Credential refresh failed ({refresh_exc}); continuing with the "
                f"original error.",
                flush=True,
            )
            return False

    def wrap_model_call(self, request, handler):
        for attempt in range(self.max_retries + 1):
            try:
                return handler(request)
            except Exception as exc:
                if self._maybe_refresh(exc):
                    try:
                        return handler(request)
                    except Exception as retry_exc:
                        exc = retry_exc
                attempts_made = attempt + 1
                if not should_retry_exception(exc, self.retry_on):
                    return self._handle_failure(exc, attempts_made)
                if attempt < self.max_retries:
                    delay = calculate_delay(
                        attempt,
                        backoff_factor=self.backoff_factor,
                        initial_delay=self.initial_delay,
                        max_delay=self.max_delay,
                        jitter=self.jitter,
                    )
                    if delay > 0:
                        time.sleep(delay)
                else:
                    return self._handle_failure(exc, attempts_made)
        raise RuntimeError("Unexpected: retry loop completed without returning")

    async def awrap_model_call(self, request, handler):
        for attempt in range(self.max_retries + 1):
            try:
                return await handler(request)
            except Exception as exc:
                if self._maybe_refresh(exc):
                    try:
                        return await handler(request)
                    except Exception as retry_exc:
                        exc = retry_exc
                attempts_made = attempt + 1
                if not should_retry_exception(exc, self.retry_on):
                    return self._handle_failure(exc, attempts_made)
                if attempt < self.max_retries:
                    delay = calculate_delay(
                        attempt,
                        backoff_factor=self.backoff_factor,
                        initial_delay=self.initial_delay,
                        max_delay=self.max_delay,
                        jitter=self.jitter,
                    )
                    if delay > 0:
                        await asyncio.sleep(delay)
                else:
                    return self._handle_failure(exc, attempts_made)
        raise RuntimeError("Unexpected: retry loop completed without returning")


DB_URI = os.getenv("POSTGRES_URI")

CONTEXT_DIR = Path(__file__).parent / "context"

sys_prompt_path = Path(__file__).parent / "reports.md"

# Local SQLite fallback (single file, two tables inside - same pattern as Postgres)
# Overridable via REPORT_AGENT_DB_PATH so tests/experiments can use a temp file.
LOCAL_DB_PATH = Path(os.environ.get("REPORT_AGENT_DB_PATH", str(Path(__file__).parent / "agent_session.db")))


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
    
    def __init__(self, debug_mode: bool = False):
        self.debug_mode = debug_mode
        self.pool = None
        self.checkpointer = None
        self.store = None
        self.agent = None
        self.langfuse = None
        self.langfuse_handler = None
        self.langfuse_ok = False
        self.system_prompt_text = None
        self.fs_middleware = None
        self.llm = None
        self.llm_small = None
        # Which backend _init_llm resolved. Set there, not here; declared so the
        # attribute exists before initialize() for code that reads it early.
        self.llm_backend = None
        self.tool_emulator = None
        self.retry_middleware = None
    
    # ==========================================
    # Public API
    # ==========================================
    
    async def initialize(self, debug_mode: bool = False, force_sqlite: bool = False):
        """
        Full initialization — called from Starlette lifespan.
        
        Delegates to private methods for each layer.
        If one layer fails, you know exactly which layer it is.
        
        Args:
            debug_mode: When True, includes LLMToolEmulator in agent middleware for testing.
            force_sqlite: When True, bypasses Postgres and always uses local SQLite.
        """
        self.debug_mode = debug_mode
        self._init_langfuse()
        self._init_system_prompt()
        self._init_llm(streaming=True)
        self._init_middleware()
        await self._init_pool(force_sqlite=force_sqlite)
        await self._create_agent(debug_mode=debug_mode)
    
    async def initialize_persistence(self):
        """
        Initialize ONLY observability + database persistence (checkpointer + store).
        
        Skips the LLM, middleware, and agent creation entirely - so no model API
        keys are required. Used by the Streamlit UI's partial swap, where the
        UI wants the real persistence backend (session checkpoints + virtual
        filesystem) but still runs the mock agent runner.
        
        Delegates to the same private methods the full ``initialize()`` uses.
        """
        self._init_langfuse()
        await self._init_pool()
    
    # ==========================================
    # Private initialization methods
    # ==========================================
    
    def _init_langfuse(self):
        """Initialize Langfuse observability client (optional, fails gracefully offline).

        ``self.langfuse_ok`` records whether the client actually authenticated -
        a disabled client (missing env keys) makes ``auth_check()`` return False
        instead of raising, which would otherwise look "connected".
        """
        try:
            self.langfuse = get_client()
            # auth_check does network I/O - guard with try so offline doesn't crash initialize()
            try:
                if self.langfuse.auth_check():
                    print("Langfuse client is authenticated and ready!", flush=True)
                    self.langfuse_ok = True
                else:
                    print("Authentication failed. Please check your credentials and host.", flush=True)
                    self.langfuse_ok = False
            except Exception as e:
                print(f"Langfuse auth_check failed ({e}), running without observability", flush=True)
                self.langfuse_handler = None
                self.langfuse_ok = False
                return
            self.langfuse_handler = CallbackHandler()
        except Exception as e:
            print(f"Langfuse init failed ({e}), running without observability", flush=True)
            self.langfuse = None
            self.langfuse_handler = None
            self.langfuse_ok = False
    
    def _init_system_prompt(self):
        """Load the system prompt from disk."""
        if not sys_prompt_path.exists():
            raise FileNotFoundError(f"Could not find prompt file at {sys_prompt_path}")
        
        # Read the entire contents of the file into a string
        self.system_prompt_text = sys_prompt_path.read_text(encoding="utf-8")
        print(f"Loaded system prompt ({len(self.system_prompt_text)} characters)", flush=True)
    # ==========================================
    # LLM Initialization (single function — all provider logic here)
    # ==========================================
    def _init_llm(self, *, streaming: bool = True):
        """Establish ALL LLMs in ONE place.

        Provider selected by ``LLM_BACKEND`` env var:
          - ``openai_local``     → ``langchain_openai.ChatOpenAI``
            (any OpenAI-compatible server — llama.cpp model server, etc.)
          - ``openrouter`` → ``langchain_openrouter.ChatOpenRouter``
          - ``mistral``    → ``langchain_mistralai.ChatMistralAI``

        If ``LLM_BACKEND`` is unset, it is inferred from which env vars are present:
          ``LLM_BASE_URL`` → openai_local, ``OPENROUTER`` → openrouter, else mistral.

        Model ids (agent / emulator) come from ``AGENT_MODEL`` /
        ``AGENT_EMULATOR_MODEL``. These two are backend-agnostic by design -
        one pair of names for every backend. Each falls back to that backend's
        default from ``_BACKEND_MODEL_DEFAULTS``, so switching ``LLM_BACKEND``
        switches the default model with it and no other edit is needed. An
        override that cannot belong to the selected backend is reported and
        ignored rather than passed through to fail later. Both models become
        instance attributes: ``self.llm`` and ``self.llm_small``.

        The resolved backend name is kept on ``self.llm_backend`` so that
        anything outside this module can report what was actually selected,
        rather than the reader having to re-derive it from the environment.
        See ``streamlit_ui/services/real_backend.py::component_status_lines``.
        """
        # --- Resolve backend ---
        backend = (os.getenv("LLM_BACKEND") or "").strip().lower()
        if not backend:
            if os.getenv("LLM_BASE_URL"):
                backend = "openai_local"
            elif os.environ.get("OPENROUTER"):
                backend = "openrouter"
            else:
                backend = "mistral"

        # Kept for anything that needs to name the selected backend without
        # re-reading the environment (status output, diagnostics, errors).
        self.llm_backend = backend

        # --- Per-backend default model id ---
        # TODO, for later, to remake nested function pattern into unified 1 function
        # since it works for now, then i'll leave it for later
        # NOTE, A table rather than a nested if-chain, so that adding a backend is one
        # row instead of a new branch in two places. Roles are "agent" (the model
        # that does the work) and "emulator" (the cheap model behind
        # LLMToolEmulator, only wired in when debug_mode is on).
        defaults = _BACKEND_MODEL_DEFAULTS.get(backend)
        if defaults is None:
            raise ValueError(
                f"Unknown LLM_BACKEND {backend!r}; expected one of "
                f"{sorted(_BACKEND_MODEL_DEFAULTS)}."
            )

        # --- Build one model instance ---
        def _build(model: str, timeout: float, max_retries: int):
            if backend == "openai_local":
                return ChatOpenAI(
                    model=model,
                    base_url=os.getenv("LLM_BASE_URL") or "http://localhost:1234/v1",
                    api_key=os.getenv("LLM_API_KEY", "").strip() or "none",
                    temperature=0.2,
                    streaming=streaming,
                    # Token counts, and why this flag has to be spelled out.
                    #
                    # `ChatOpenAI.stream_usage` defaults to None, and
                    # langchain-openai only auto-flips None to True when the
                    # client is talking to the DEFAULT OpenAI base URL
                    # (langchain_openai/chat_models/base.py, the block guarded
                    # by "Enable stream_usage by default if using default base
                    # URL"). The line above always passes an explicit
                    # `base_url`, so that auto-enable never fires and the flag
                    # stays off. With it off, no `stream_options` is sent on the
                    # wire at all, the server has no reason to emit a usage
                    # chunk, and every generation lands in Langfuse as
                    # promptTokens 0 / usageDetails {} - a silent zero, which is
                    # what this used to do.
                    #
                    # Passing True explicitly overrides the auto-enable entirely;
                    # it is not a workaround, it is the intended way to turn the
                    # feature on for a custom endpoint. Verified on the wire:
                    # the outgoing body then carries
                    # stream_options={"include_usage": true} and a llama.cpp
                    # server replies with a usage chunk before [DONE]. See
                    # tests/experiments/exp_token_usage.py, which also runs the
                    # stream_usage=False control to prove the flag is what makes
                    # the difference.
                    #
                    # Note the counts arrive on an INTERMEDIATE streamed chunk,
                    # not the last one - chunks[-1].usage_metadata is None. And
                    # the OpenAI field names are renamed on the way in:
                    # prompt_tokens -> input_tokens, completion_tokens ->
                    # output_tokens. Cached prompt tokens are preserved
                    # separately as input_token_details.cache_read rather than
                    # folded in, and the Langfuse handler subtracts them from
                    # `input` so its figure is the billable one, not the
                    # headline. This is the only place token accounting exists
                    # today; nothing in this repo reads the counts.
                    stream_usage=True,
                    timeout=timeout,
                    rate_limiter=None,
                    max_retries=max_retries,
                )
            if backend == "openrouter":
                # ChatOpenRouter uses TIMEOUT in milliseconds, not seconds.
                return ChatOpenRouter(
                    model=model,
                    api_key=os.environ.get("OPENROUTER"),
                    temperature=0.2,
                    streaming=streaming,
                    timeout=int(timeout * 1000),
                    rate_limiter=None,
                    max_retries=max_retries,
                )
            if backend == "mistral":
                return ChatMistralAI(
                    model=model,
                    temperature=0.2,
                    streaming=streaming,
                    timeout=timeout,
                    rate_limiter=None,
                    max_retries=max_retries,
                )
            raise ValueError(f"Unknown LLM_BACKEND: {backend!r} (use 'openai_local', 'openrouter', or 'mistral')")

        # --- Create both models ---
        def _resolve(role: str, env_var: str) -> str:
            """Pick the model id for `role`: env override, else backend default."""
            override = (os.getenv(env_var) or "").strip()
            if not override:
                return defaults[role]
            # Catch the cross-backend leftover here rather than letting it reach
            # the provider. It costs nothing when the override is fine, and when
            # it is not, the message says which variable to change instead of
            # leaving a 404 to be interpreted from a stack trace mid-conversation.
            if backend in _BACKEND_NAMESPACED_MODEL_IDS and "/" not in override:
                print(
                    f"WARNING: {env_var}={override!r} is not a valid model id for "
                    f"backend {backend!r}, whose ids are namespaced "
                    f"'<vendor>/<model>' (e.g. {defaults[role]!r}). "
                    f"Using the {backend} default instead. This usually means the "
                    f"value was left over from a different LLM_BACKEND - either "
                    f"namespaced it, or unset {env_var} to use the default.",
                    flush=True,
                )
                return defaults[role]
            return override

        self.llm      = _build(_resolve("agent", "AGENT_MODEL"), 180.0, 3)
        self.llm_small = _build(_resolve("emulator", "AGENT_EMULATOR_MODEL"), 60.0, 0)

        print(
            f"Resolved LLM: {type(self.llm).__name__} "
            f"model={getattr(self.llm, 'model', None) or getattr(self.llm, 'model_name', None)} "
            f"(backend={backend})",
            flush=True,
        )

    def _init_middleware(self):
        """Configure middleware ONLY (LLMs are created in ``_init_llm``)."""
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
        
        # Rate limiter - constructed, deliberately NOT wired into any model.
        #
        # This is not dead code and should not be deleted. It exists because the
        # mistral backend needs request pacing; the local model server and
        # OpenRouter do not, which is why every branch of _init_llm passes
        # rate_limiter=None. It is kept here so it can be re-enabled per backend
        # without re-deriving it.
        #
        # If you wire it up, wire it per backend rather than globally: a limiter
        # that slows the local backend down to one request per 20s would be a
        # serious regression, and that is exactly the mistake this comment exists
        # to prevent. docs/architecture.md records the same decision.
        rate_limiter = InMemoryRateLimiter(
            requests_per_second=0.05,  # 1 request every 20 seconds
            check_every_n_seconds=0.5,   # Check twice a second if we are allowed to proceed
            max_bucket_size=1,           # No bursting; strict 1-at-a-time limit
        )

        # LLM Tool Emulator (Intercepts ONLY these tools for testing)
        # NOTE: The emulator is only added to agent middleware when debug_mode=True
        self.tool_emulator = LLMToolEmulator(
            tools=["export_report_to_local_disk"],
            model=self.llm_small # fast/cheap model to generate the mock responses
        )

        # model request retry middleware, catches api exception and repeats the requests with intervals
        # the timer should be small, otherwise this stuck with rate_limiter and everything breaks
        self.retry_middleware = CredentialRefreshRetryMiddleware(
            refresh=self._reload_llm_credential,
            max_retries=3,
            retry_on=(Exception,), # Catch standard API errors
            # 'error' re-raises the underlying exception once the retry budget is
            # spent. The value that was here before, 'raise', is not recognised:
            # _handle_failure only re-raises for 'error' or a callable, and
            # anything else formats the failure into a ModelResponse holding an
            # AIMessage. That made a dead key surface as the agent apparently
            # answering with an error string, which is why a rotated credential
            # (#13) looked like a mysterious bad reply instead of a failure.
            on_failure='error',
            backoff_factor=2.0,  
            initial_delay=2.0, # Wait 5s, then 10s, then 15 before retrying
            max_delay=10.0, # Will wait up to 60 seconds between retries if the API is struggling
            jitter=True
        )

    def _reload_llm_credential(self) -> bool:
        """Re-read the chat credential and push it into the live clients.

        Called when a model call fails with an auth error, to recover from a
        credential that changed underneath a running process. Returns True when
        a live client was actually updated.

        Why in-place rather than rebuilding the model: ``create_agent`` binds
        ``model=self.llm`` when the graph is compiled, so a freshly constructed
        ChatOpenAI would never be the object the graph calls. But the openai
        clients resolve ``api_key`` per request (they build ``auth_headers`` on
        each call), so assigning to it takes effect on the next request without
        touching the compiled graph, the middleware stack, or the emulator that
        captured ``llm_small`` at construction.

        Both clients are updated. ChatOpenAI keeps a separate ``root_client``
        and ``root_async_client``, and this agent is async end to end, so
        updating only the sync one changes nothing at runtime.

        The value comes from the ``.env`` FILE, preferring it over
        ``os.environ``. That is the #13 case - a user editing ``.env`` while a
        long-running server holds the old value - and it removes the "sync the
        key, then restart the process" step.

        Only the openai family is handled - openai_local and openrouter both
        build on the openai SDK, whose clients expose the key as a plain
        attribute. Mistral keeps its key inside its own client object with no
        equivalent handle, so a mistral 401 falls through to the normal retry
        path and fails as before.
        """
        if self.llm_backend == "mistral":
            return False

        # Read the key from the .env FILE, not from os.environ. load_dotenv()
        # ran once at import, so os.environ holds the value as it was at
        # startup and re-reading it here would return the stale key. But
        # load_dotenv(override=True) is the wrong tool for the same reason: it
        # makes .env outrank the live environment, which would silently undo any
        # value a launcher had already put there.
        #
        # Precedence: the .env file if it defines the key, else os.environ. That
        # is the #13 case - a user editing .env under a running server - without
        # demoting every other source.
        env_var = "OPENROUTER" if self.llm_backend == "openrouter" else "LLM_API_KEY"
        new_key = None
        try:
            dotenv_path = find_dotenv()
            if dotenv_path:
                from_file = dotenv_values(dotenv_path).get(env_var)
                if from_file is not None:
                    new_key = from_file.strip()
        except Exception as exc:  # pragma: no cover - fall back to os.environ
            print(
                f"Could not read {env_var} from .env ({exc}); using the "
                f"environment value.",
                flush=True,
            )

        if new_key is None:
            new_key = (os.environ.get(env_var) or "").strip()
        if not new_key:
            new_key = "none"

        updated = False
        for model in (self.llm, self.llm_small):
            # BOTH clients have to be updated. ChatOpenAI builds a sync
            # `root_client` and a separate async `root_async_client`, and the
            # agent only ever runs async (LangGraph is async throughout, and the
            # Streamlit UI crosses an EventLoopThread). Updating only the sync
            # one looks correct in a unit test with a fake client and changes
            # nothing at runtime - the request keeps sending the old key.
            for attr in ("root_client", "root_async_client"):
                client = getattr(model, attr, None)
                if client is None or getattr(client, "api_key", None) == new_key:
                    continue
                client.api_key = new_key
                updated = True

        if updated:
            print(
                f"Auth failed; reloaded {env_var} from .env into the "
                f"{self.llm_backend} client(s). Retrying the request.",
                flush=True,
            )
        return updated
    
    async def _init_pool(self, force_sqlite: bool = False):
        """Initialize database pool, checkpointer, and store.
        
        Tries remote Postgres first (if POSTGRES_URI is set and reachable).
        Falls back to local SQLite (single file, two tables) for offline/dev.
        
        Args:
            force_sqlite: When True, bypasses Postgres entirely and uses local SQLite
                          even when POSTGRES_URI is configured and reachable.
        """
        # Env override so any caller (Streamlit UI launch, scripts, tests) can
        # force the local backend without making Postgres unreachable or editing
        # .env - in cmd `set POSTGRES_URI=` would UNSET the var and let .env win:
        #   set REPORT_AGENT_FORCE_SQLITE=1
        if not force_sqlite:
            force_sqlite = os.getenv("REPORT_AGENT_FORCE_SQLITE", "").strip().lower() in ("1", "true", "yes")
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
        
        # --- Try Postgres if DB_URI is configured (and not forced to SQLite) ---
        if DB_URI and not force_sqlite:
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
                print(f"Using Postgres backend: {DB_URI[:30]}...", flush=True)
                return
            except Exception as e:
                print(f"Postgres unreachable ({e}), falling back to local SQLite: {LOCAL_DB_PATH}", flush=True)
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
        print(f"Using local SQLite backend: {LOCAL_DB_PATH}", flush=True)
    
    async def ensure_pool(self) -> bool:
        """Reopen the DB pool if it is missing or broken (transient remote drops).

        Local SQLite mode (pool None) needs no healing. After a reopen the
        agent is rebuilt against the fresh checkpointer/store, so callers must
        refresh their own agent handle — the same thread_id then resumes from
        its last checkpoint instead of failing the whole run.
        Returns True if a reopen happened.
        """
        if self.pool is None:
            return False
        try:
            async with self.pool.connection(timeout=3.0):
                pass
            return False
        except Exception as e:
            print(f"Pool unhealthy ({e}), reopening...")
            try:
                await self.pool.close()
            except Exception:
                pass
            self.pool = None
            await self._init_pool()
            await self._create_agent(debug_mode=self.debug_mode)
            return True

    async def _create_agent(self, debug_mode: bool = False):
        """Create the LangChain agent with active middleware construction.
        
        Middleware is built conditionally based on runtime flags:
          - fs_middleware: always included (persistent virtual filesystem).
          - LLMToolEmulator: included ONLY when ``debug_mode`` is enabled.
          - retry_middleware: always included (model request retries).
        """
        middleware = [self.fs_middleware, self.retry_middleware]
        if debug_mode and self.tool_emulator is not None:
            middleware.insert(1, self.tool_emulator)
        
        self.agent = create_agent(
            model=self.llm,
            system_prompt=self.system_prompt_text,
            tools=[export_report_to_local_disk],
            #checkpointer=InMemorySaver()
            store=self.store,
            middleware=middleware,
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
