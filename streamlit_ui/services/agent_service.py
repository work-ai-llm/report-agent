"""PenTestAgentService - Core service layer for Streamlit UI.

This module provides the business logic layer that connects the Streamlit UI
to the underlying agent system, file storage, and session persistence.

## Data Flow Architecture

```
┌─────────────────┐
│  Streamlit UI   │
│  (app.py)       │
└────────┬────────┘
         │ User actions (upload, send message, download)
         ▼
┌─────────────────────────────────────────┐
│     PenTestAgentService                 │
│  - upload_finding()                     │
│  - send_message()                       │
│  - download_report()                    │
│  - list_findings()                      │
└────────┬────────────────┬───────────────┘
         │                │
         │ Injected       │ Injected
         ▼                ▼
┌─────────────────┐  ┌──────────────────┐
│ Checkpointer    │  │ FileStore        │
│ (session state) │  │ (virtual FS)     │
└─────────────────┘  └──────────────────┘
         │
         │ Injected
         ▼
┌─────────────────┐
│ AgentRunner     │
│ (LLM execution) │
└─────────────────┘
```

## Dependency Injection

The service accepts three protocol-based dependencies:
1. **checkpointer**: Persists conversation history per thread_id
2. **file_store**: Global filesystem for findings and reports
3. **agent_runner**: Executes agent with streaming events

Production uses PostgreSQL implementations; tests use mocks.

## Thread Safety

This service is designed for single-threaded Streamlit execution.
Each Streamlit session gets its own service instance stored in
st.session_state.
"""

from typing import Optional, List, Dict, Any, Iterator
import time

from streamlit_ui.services.interfaces import (
    CheckpointerProtocol,
    FileStoreProtocol,
    AgentRunnerProtocol,
)
from streamlit_ui.services.run_registry import REGISTRY, RunBusyError

# How often send_message() checks the run registry for new events while a turn
# is in flight. Small enough to feel live, large enough not to spin a core.
_RUN_POLL_INTERVAL = 0.02


def _strip_prefix(paths: List[str], prefix: str) -> List[str]:
    """Drop a leading path prefix from each entry - and only a leading one.

    These two methods used ``str.replace``, which also rewrote the same text
    anywhere else in the name: ``findings/findings_summary.txt`` displayed as
    ``summary.txt``, and a later write using that displayed name targeted a
    different key. Leading slashes are dropped too, because store keys are
    slash-prefixed while the caller passes a bare prefix.
    """
    stripped = []
    for path in paths:
        bare = path.lstrip("/")
        stripped.append(bare[len(prefix):] if bare.startswith(prefix) else bare)
    return stripped


class PenTestAgentService:
    """Service layer connecting Streamlit UI to agent system.
    
    This class encapsulates all business logic for the pen-test reporting UI,
    providing clean methods for file operations, agent interaction, and
    session management.
    
    ## Usage Example
    ```python
    # Production setup
    service = PenTestAgentService(
        thread_id="session_123",
        checkpointer=postgres_saver,
        file_store=postgres_store,
        agent_runner=langchain_agent_wrapper
    )
    
    # Test setup
    service = PenTestAgentService(
        thread_id="test_session",
        checkpointer=MockCheckpointer(),
        file_store=MockFileStore(),
        agent_runner=MockAgentRunner()
    )
    
    # Use service methods
    service.upload_finding("nmap.txt", b"Port 80 open")
    for event in service.send_message("Analyze findings"):
        render_event(event)
    report = service.download_report("report.md")
    ```
    """
    
    def __init__(
        self,
        thread_id: str,
        checkpointer: CheckpointerProtocol,
        file_store: FileStoreProtocol,
        agent_runner: AgentRunnerProtocol,
    ):
        """Initialize service with injected dependencies.
        
        Args:
            thread_id: Unique identifier for this session/thread
            checkpointer: Session persistence implementation
            file_store: Virtual filesystem implementation
            agent_runner: Agent execution implementation
        """
        self._thread_id = thread_id
        self._checkpointer = checkpointer
        self._file_store = file_store
        self._agent_runner = agent_runner
    
    @property
    def thread_id(self) -> str:
        """Get the current thread/session ID."""
        return self._thread_id
    
    def upload_finding(self, filename: str, content: bytes) -> None:
        """Upload a findings file to the global virtual filesystem.
        
        Findings are stored under the 'findings/' prefix and are visible
        across all sessions (global namespace).
        
        Args:
            filename: Name of the findings file (e.g., "nmap_scan.txt")
            content: Raw file content as bytes
            
        Data Flow:
            UI upload button → upload_finding() → file_store.write_file()
        """
        path = f"findings/{filename}"
        self._file_store.write_file(path, content)
    
    def list_findings(self) -> List[str]:
        """List all uploaded findings files.
        
        Returns:
            List of filenames (without 'findings/' prefix)
            
        Data Flow:
            UI sidebar → list_findings() → file_store.list_files() → UI display
        """
        return _strip_prefix(
            self._file_store.list_files(prefix="findings/"), "findings/"
        )
    
    def list_filesystem(self) -> List[str]:
        """List every file the agent can read, grouped-ready display paths.
        
        This is the whole of the virtual filesystem, not just findings and
        reports: the context library the agent is told to read lives under
        the same prefix as everything else. The store keys arrive without the
        ``/context/`` root that the routing layer strips before storing, so it
        is put back here - the agent says ``/context/findings/nmap.txt`` when it
        lists that directory, and the sidebar should not disagree with it.
        
        Does NOT include ``/scratchpad/``. That half is held by an in-memory
        backend in the agent's checkpoint namespace and is unreachable from a
        store listing; see list_scratchpad().
        
        Returns:
            Sorted display paths, e.g. ``/context/findings/nmap.txt``
        """
        paths = self._file_store.list_files()
        return sorted(f"/context/{p.lstrip('/')}" for p in paths)
    
    def list_scratchpad(self) -> Dict[str, str]:
        """List the current session's agent working files.
        
        The agent's filesystem is a composite: ``/context/`` is persisted in the
        store, and everything else - the scratchpad it drafts into - lives in an
        in-memory backend under a state channel in the agent's own checkpoint
        namespace. A store listing returns none of it at any prefix, so this
        reads that channel instead.
        
        Files persist across turns of a session rather than clearing per run,
        so this is the whole session's scratchpad unless it is filtered.
        
        Returns:
            Mapping of path to text content; empty before the first agent run
        """
        return self._checkpointer.list_agent_files(
            self._thread_id, prefix="scratchpad/"
        )
    
    def start_turn(self, message: str) -> Dict[str, Any]:
        """Begin a turn and return immediately. Never blocks on the agent.

        This is what the UI uses. The alternative - iterating
        :meth:`send_message` inside the Streamlit script run - keeps a script run
        alive for the whole turn, which has two costs that only became visible
        once the run registry existed:

        1. Streamlit shows its global "app running" widget with a Stop button once
           a script run has been going for 500ms, and that widget's Stop calls
           ``request_script_stop`` - the *same* call a widget click makes
           internally under ``runner.fastReruns``. Having a long-lived script run
           therefore makes "the user stopped this" indistinguishable from "the
           user clicked something", which is why cancellation has to be explicit.
        2. Any widget interaction queues a rerun, which unwinds the script run
           mid-turn. Nothing is lost (the run persists itself), but the display
           stops until something triggers another render.

        Starting the turn and returning leaves the script run short, so neither
        applies. The turn's progress is read from the run registry instead.

        Args:
            message: User input message.

        Returns:
            A snapshot dict of the new run.

        Raises:
            RunBusyError: a turn for this thread is already in flight. Callers
                should check :meth:`is_run_active` first; this is the backstop.
        """
        if getattr(self._agent_runner, "supports_polling", None) and self._agent_runner.supports_polling():
            state = self._agent_runner.start(message, self._thread_id)
            return state.snapshot()
        # A runner with no registry support has to be driven synchronously, or
        # there is nothing to read progress from.
        for _event in self.send_message(message):
            pass
        return {}

    def send_message(self, message: str) -> Iterator[Dict[str, Any]]:
        """Send a message to the agent and stream events back.
        
        This method executes the agent with the user's message and yields
        events in real-time (tool calls, tool results, final response).
        
        Args:
            message: User input message to process
            
        Yields:
            Event dictionaries from agent runner:
            - {"type": "tool_call", "data": {...}}
            - {"type": "tool_result", "data": {...}}
            - {"type": "message", "data": "response text"}
            
        Data Flow:
            User input → send_message() → agent_runner.run() → yield events → UI render
            
        Example:
            ```python
            for event in service.send_message("Scan target 10.0.0.1"):
                if event["type"] == "tool_call":
                    st.json(event["data"])  # Show tool call details
                elif event["type"] == "message":
                    st.write(event["data"])  # Show final response
            ```
        """
        # Prefer the runner's rerun-safe start/poll pair when it has one. Going
        # through the registry is what makes the turn survive a Streamlit rerun:
        # the run is recorded and persisted by the runner's own thread, so this
        # generator is only a *reader*. A runner without it (a bare two-arg
        # MockAgentRunner, say) falls back to its plain generator.
        if getattr(self._agent_runner, "supports_polling", None) and self._agent_runner.supports_polling():
            try:
                self._agent_runner.start(message, self._thread_id)
            except RunBusyError as busy:
                # One turn per thread. Two would interleave their writes to the
                # same UI-namespace checkpoint row and their astream calls on the
                # same LangGraph thread - see RunBusyError. Surfaced as an event
                # rather than raised, because the caller is a UI render loop and a
                # second turn should read as "busy", not as a crash.
                yield {"type": "busy", "data": str(busy)}
                return
            index = 0
            while True:
                events, index, _revision = self._agent_runner.poll(self._thread_id, index)
                for event in events:
                    yield event
                if not self._agent_runner.is_running(self._thread_id):
                    # Drain anything recorded between the poll and the status
                    # check, then stop.
                    events, index, _revision = self._agent_runner.poll(self._thread_id, index)
                    for event in events:
                        yield event
                    return
                time.sleep(_RUN_POLL_INTERVAL)
        yield from self._agent_runner.run(message, self._thread_id)
    
    def download_report(self, filename: str) -> Optional[bytes]:
        """Download a report file from the virtual filesystem.
        
        Reports are stored under the 'reports/' prefix and can be
        generated by the agent during execution.
        
        Args:
            filename: Name of the report file (e.g., "pentest_report.md")
            
        Returns:
            Report content as bytes, or None if not found
            
        Data Flow:
            UI download button → download_report() → file_store.read_file() → browser download
        """
        path = f"reports/{filename}"
        return self._file_store.read_file(path)
    
    def list_reports(self) -> List[str]:
        """List all available report files.
        
        Returns:
            List of report filenames (without 'reports/' prefix)
            
        Data Flow:
            UI download dropdown → list_reports() → file_store.list_files() → UI options
        """
        files = self._file_store.list_files(prefix="reports/")
        return _strip_prefix(files, "reports/")
    
    def get_chat_history(self) -> List[Dict[str, Any]]:
        """Retrieve conversation history for current thread.
        
        Returns:
            List of message dictionaries with 'role' and 'content' keys,
            or empty list if no history exists
            
        Data Flow:
            UI initialization → get_chat_history() → checkpointer.get() → UI display
        """
        state = self._checkpointer.get(self._thread_id)
        if state is None:
            return []
        messages = state.get("messages", [])
        # Copy before handing out. The UI appends to (and annotates) what it gets
        # back - `app.py` appends the user row to this list, and the runner
        # annotates tool_call rows in place with their result. An in-memory
        # checkpointer returns its stored list by reference, so handing that out
        # directly would let the UI mutate persisted state with no write at all,
        # and the next run would read its own edits back as history. Rows are
        # copied too, since `result` is added in place.
        return [dict(m) if isinstance(m, dict) else m for m in messages]
    
    def save_chat_history(self, messages: List[Dict[str, Any]]) -> None:
        """Persist conversation history for current thread.
        
        Args:
            messages: List of message dictionaries to save
            
        Data Flow:
            After agent response → save_chat_history() → checkpointer.put()
        """
        state = {"messages": messages}
        self._checkpointer.put(self._thread_id, state)
    
    def list_threads(self) -> List[str]:
        """List all available session/thread IDs.

        Returns:
            List of thread_id strings for the session selector dropdown.
            Delegates to the injected checkpointer.
        """
        return self._checkpointer.list_threads()

    def delete_thread(self, thread_id: str) -> bool:
        """Delete a session/thread and its persisted conversation state.

        Args:
            thread_id: Unique identifier for the session/thread to delete

        Returns:
            True if the thread existed and was deleted, False otherwise
        """
        deleted = self._checkpointer.delete(thread_id)
        if deleted:
            # Drop the run entry too, so a deleted session cannot still report
            # "running" in the UI.
            try:
                REGISTRY.drop(thread_id)
            except Exception:  # noqa: BLE001 - never fail a delete over bookkeeping
                pass
        return deleted

    # -- run state ----------------------------------------------------------
    #
    # The agent turn is owned by the runner (and the run registry), not by the
    # Streamlit script run. These let the UI ask "has anything changed since I
    # last looked?" without touching the database and without blocking, which is
    # what lets a rerun pick up a turn that finished while the script was dead.

    def run_revision(self) -> int:
        """Monotonic change counter for this thread's latest run (``0`` if none).

        Cheap: an in-memory read. The UI compares it against the value it last
        rendered and re-reads history only when it moved.
        """
        return REGISTRY.revision(self._thread_id)

    def is_run_active(self) -> bool:
        """True while an agent turn for this thread is still in flight."""
        return REGISTRY.is_running(self._thread_id)

    def cancel_run(self) -> bool:
        """Ask this thread's in-flight turn to stop. False if none is running.

        Explicit by necessity, not by preference. Streamlit 1.62 raises the same
        ``StopException`` for the browser's Stop button and for an ordinary widget
        click (``runner.fastReruns`` makes a widget click take the
        ``request_stop`` path), so the script cannot tell "the user stopped this"
        from "the user clicked something". Inferring cancellation from the
        consumer going away would kill every turn the moment a sidebar widget was
        touched. A stopped turn emits no final message and is not persisted as
        finished.
        """
        return REGISTRY.request_cancel(self._thread_id)

    def active_run_snapshot(self) -> Optional[Dict[str, Any]]:
        """A consistent copy of this thread's latest run, or ``None``."""
        state = REGISTRY.get(self._thread_id)
        return state.snapshot() if state is not None else None

    def delete_finding(self, filename: str) -> bool:
        """Delete a findings file from the virtual filesystem.
        
        Args:
            filename: Name of the findings file to delete
            
        Returns:
            True if deleted successfully, False if not found
            
        Data Flow:
            UI delete button → delete_finding() → file_store.delete_file() → UI refresh
        """
        path = f"findings/{filename}"
        return self._file_store.delete_file(path)
