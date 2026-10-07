"""Mock implementations for testing Streamlit UI without external dependencies.

This module provides in-memory implementations of the protocols defined in
interfaces.py, allowing comprehensive testing without PostgreSQL, LLM APIs,
or any other external services.

## Usage Pattern

```python
# Create mock instances
mock_checkpointer = MockCheckpointer()
mock_file_store = MockFileStore()
mock_agent = MockAgentRunner()

# Inject into service
service = PenTestAgentService(
    thread_id="test_session",
    checkpointer=mock_checkpointer,
    file_store=mock_file_store,
    agent_runner=mock_agent
)

# Configure mock behavior
mock_agent.set_tool_calls([{"name": "nmap_scan", "args": {"target": "10.0.0.1"}}])
mock_file_store.prepopulate_with_report("report.md", b"# Penetration Test Report")

# Run tests - all operations stay in memory
```

## Thread Safety Note

These mocks use simple dictionaries and are NOT thread-safe. They are designed
for single-threaded unit tests only. For concurrent tests, create separate
instances per test case.

## Permissiveness Caveat

These mocks are self-respecting dictionaries: they accept ANY content, never
reject or transform input, and make no structural guarantees. Tests built on
them only prove the service layer works against in-memory storage (path
prefixes, threading, defaults). Real LangGraph/Postgres objects have different
method signatures than these protocols (e.g. aget_tuple/aput vs get/put), so
swapping in production implementations is NOT drop-in - adapters are required
and must be validated by an integration test against a real backend, not by
these mocks.
"""

from typing import Optional, List, Dict, Any, Iterator, Tuple

# Scratch space for tests that must drive the app through a Streamlit script.
#
# ``AppTest.from_function`` writes the function to a temporary file and execs it
# standalone, so closures and module globals of the *test* module are not visible
# inside it. The harness imports this module instead, which is importable from
# anywhere ``pythonpath = ["."]`` applies.
HARNESS: Dict[str, Any] = {}
import time
from streamlit_ui.services.interfaces import (
    CheckpointerProtocol,
    FileStoreProtocol,
    AgentRunnerProtocol,
)


class MockCheckpointer:
    """In-memory implementation of CheckpointerProtocol for testing.
    
    Stores conversation state in a simple dictionary keyed by thread_id.
    Perfect for testing session switching and state persistence logic.
    
    ## Example
    ```python
    cp = MockCheckpointer()
    cp.put("session_1", {"messages": [{"role": "user", "content": "hello"}]})
    state = cp.get("session_1")  # Returns the saved dict
    threads = cp.list_threads()  # Returns ["session_1"]
    ```
    """
    
    def __init__(self):
        """Initialize empty in-memory storage."""
        self._storage: Dict[str, Dict[str, Any]] = {}
    
    def get(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve conversation state for a given thread.
        
        Args:
            thread_id: Unique identifier for the session/thread
            
        Returns:
            Saved state dictionary or None if not found
        """
        return self._storage.get(thread_id)
    
    def put(self, thread_id: str, state: Dict[str, Any]) -> None:
        """Save conversation state for a given thread.
        
        Args:
            thread_id: Unique identifier for the session/thread
            state: Conversation state to persist
        """
        self._storage[thread_id] = state
    
    def list_threads(self) -> List[str]:
        """List all available thread IDs.
        
        Returns:
            List of thread_id strings for session selector dropdown
        """
        return list(self._storage.keys())
    
    def delete(self, thread_id: str) -> bool:
        """Delete a thread and its stored conversation state.
        
        Args:
            thread_id: Unique identifier for the session/thread to delete
            
        Returns:
            True if the thread existed and was deleted, False otherwise
        """
        if thread_id in self._storage:
            del self._storage[thread_id]
            return True
        return False
    
    def clear(self) -> None:
        """Clear all stored threads (useful for test cleanup)."""
        self._storage.clear()


class MockFileStore:
    """In-memory implementation of FileStoreProtocol for testing.
    
    Stores files in a dictionary keyed by path. Supports pre-population
    for simulating existing reports and findings before tests run.
    
    ## Example
    ```python
    fs = MockFileStore()
    fs.write_file("findings/nmap.txt", b"Port 80 open")
    content = fs.read_file("findings/nmap.txt")  # Returns b"Port 80 open"
    files = fs.list_files("findings/")  # Returns ["findings/nmap.txt"]
    ```
    """
    
    def __init__(self):
        """Initialize empty in-memory file storage."""
        self._files: Dict[str, bytes] = {}
    
    def read_file(self, path: str) -> Optional[bytes]:
        """Read file content from virtual filesystem.
        
        Args:
            path: File path (e.g., "findings/nmap.txt")
            
        Returns:
            File content as bytes or None if not found
        """
        return self._files.get(path)
    
    def write_file(self, path: str, content: bytes) -> None:
        """Write file content to virtual filesystem.
        
        Args:
            path: File path (e.g., "findings/nmap.txt")
            content: File content as bytes
        """
        self._files[path] = content
    
    def list_files(self, prefix: str = "") -> List[str]:
        """List files matching optional prefix.
        
        Args:
            prefix: Optional path prefix to filter results (e.g., "findings/")
            
        Returns:
            List of file paths matching the prefix
        """
        if prefix:
            return [path for path in self._files.keys() if path.startswith(prefix)]
        return list(self._files.keys())
    
    def delete_file(self, path: str) -> bool:
        """Delete file from virtual filesystem.
        
        Args:
            path: File path to delete
            
        Returns:
            True if deleted, False if not found
        """
        if path in self._files:
            del self._files[path]
            return True
        return False
    
    def prepopulate_with_report(self, filename: str, content: bytes) -> None:
        """Helper method to pre-load a report for download tests.
        
        Args:
            filename: Report filename (will be stored under "reports/" prefix)
            content: Report content as bytes
        """
        path = f"reports/{filename}" if not filename.startswith("reports/") else filename
        self._files[path] = content
    
    def prepopulate_with_finding(self, filename: str, content: bytes) -> None:
        """Helper method to pre-load a finding for upload tests.
        
        Args:
            filename: Finding filename (will be stored under "findings/" prefix)
            content: Finding content as bytes
        """
        path = f"findings/{filename}" if not filename.startswith("findings/") else filename
        self._files[path] = content
    
    def clear(self) -> None:
        """Clear all stored files (useful for test cleanup)."""
        self._files.clear()


class MockAgentRunner:
    """In-memory implementation of AgentRunnerProtocol for testing.
    
    Simulates agent execution by yielding pre-programmed events.
    Allows testing of UI streaming logic without actual LLM calls.
    
    ## Example
    ```python
    agent = MockAgentRunner()
    agent.set_tool_calls([
        {"name": "nmap_scan", "args": {"target": "10.0.0.1"}},
        {"name": "nikto_scan", "args": {"url": "http://10.0.0.1"}}
    ])
    
    events = list(agent.run("Scan target", "session_1"))
    # Yields tool_call events for each configured tool, then final message
    ```
    """
    
    def __init__(self, delay_seconds: float = 0.0, checkpointer: Any = None):
        """Initialize with empty tool call list.

        Args:
            delay_seconds: Optional per-event delay to simulate real LLM
                streaming latency (used by the demo app, not by tests).
            checkpointer: Optional ``CheckpointerProtocol``. When supplied, the
                runner also exposes the rerun-safe :meth:`start` / :meth:`poll`
                pair and persists each turn itself. When omitted, :meth:`run`
                stays a plain in-process generator with no threads involved.
        """
        self._tool_calls: List[Dict[str, Any]] = []
        self._final_message: str = "This is a mock agent response."
        self.last_message: Optional[str] = None
        self.last_thread_id: Optional[str] = None
        self._delay_seconds: float = delay_seconds
        self._checkpointer = checkpointer
        # Number of turns started via start(); lets a test assert that a second
        # submit did not launch a second run.
        self.start_count: int = 0
    
    def set_tool_calls(self, tool_calls: List[Dict[str, Any]]) -> None:
        """Configure which tool calls the mock should emit.
        
        Args:
            tool_calls: List of tool call dictionaries with 'name' and 'args' keys
        """
        self._tool_calls = tool_calls
    
    def set_final_message(self, message: str) -> None:
        """Configure the final response message.
        
        Args:
            message: The message to yield after all tool calls
        """
        self._final_message = message
    
    def run(self, message: str, thread_id: str) -> Iterator[Dict[str, Any]]:
        """Execute agent with streaming event output.
        
        Yields pre-configured tool calls followed by final message.
        Ignores input parameters - behavior is fully controlled by set_tool_calls().
        However, the inputs are captured on last_message/last_thread_id so tests
        can assert the caller forwarded the correct values.
        
        Args:
            message: User input message (ignored in mock)
            thread_id: Session identifier (ignored in mock)
            
        Yields:
            Event dictionaries:
            - {"type": "tool_call", "data": {"name": ..., "args": ...}}
            - {"type": "message", "data": "final response text"}
        """
        self.last_message = message
        self.last_thread_id = thread_id
        
        # Yield tool call events
        for tool_call in self._tool_calls:
            if self._delay_seconds:
                time.sleep(self._delay_seconds)
            yield {
                "type": "tool_call",
                "data": {
                    "name": tool_call.get("name", "unknown_tool"),
                    "args": tool_call.get("args", {})
                }
            }
        
        # Yield final message
        if self._delay_seconds:
            time.sleep(self._delay_seconds)
        yield {
            "type": "message",
            "data": self._final_message
        }

    # -- rerun-safe API -----------------------------------------------------
    #
    # Mirrors RealAgentRunnerAdapter: the turn is recorded in the process-wide
    # run registry and persisted from a worker thread, so a Streamlit rerun that
    # kills the script run cannot lose the turn. Only available when a
    # checkpointer was injected; without one there is nowhere to persist to, and
    # run() stays a plain generator.

    def supports_polling(self) -> bool:
        """True when :meth:`start` / :meth:`poll` are usable."""
        return self._checkpointer is not None

    def start(self, message: str, thread_id: str) -> Any:
        """Begin a turn on a worker thread and return its ``RunState``."""
        if self._checkpointer is None:
            raise RuntimeError("start() requires an injected checkpointer")
        from streamlit_ui.services.run_registry import REGISTRY, spawn

        self.start_count += 1
        stored = self._checkpointer.get(thread_id) or {}
        existing = [dict(r) if isinstance(r, dict) else r for r in stored.get("messages", [])]
        state = REGISTRY.start(
            thread_id, message, existing + [{"type": "user", "content": message}]
        )
        self._checkpointer.put(thread_id, {"messages": list(state.messages)})
        spawn(
            state,
            lambda: self._produce(message, thread_id),
            persist=lambda msgs: self._checkpointer.put(thread_id, {"messages": msgs}),
            name=f"mock-agent-{thread_id}",
        )
        return state

    def poll(self, thread_id: str, index: int) -> Tuple[List[Dict[str, Any]], int, int]:
        """Return ``(events, next_index, revision)`` for incremental rendering."""
        from streamlit_ui.services.run_registry import REGISTRY

        state = REGISTRY.get(thread_id)
        if state is None:
            return [], index, 0
        events, next_index = state.events_since(index)
        return events, next_index, state.revision

    def active_run(self, thread_id: str) -> Any:
        from streamlit_ui.services.run_registry import REGISTRY

        return REGISTRY.get(thread_id)

    def is_running(self, thread_id: str) -> bool:
        from streamlit_ui.services.run_registry import REGISTRY

        return REGISTRY.is_running(thread_id)

    def run_revision(self) -> int:
        return 0

    def _produce(
        self, message: str, thread_id: str
    ) -> Iterator[Tuple[Dict[str, Any], List[Dict[str, Any]]]]:
        """Yield ``(event, rows)`` pairs, appending rows in the UI's shapes."""
        self.last_message = message
        self.last_thread_id = thread_id
        for tool_call in self._tool_calls:
            if self._delay_seconds:
                time.sleep(self._delay_seconds)
            name = tool_call.get("name", "unknown_tool")
            args = tool_call.get("args", {})
            yield (
                {"type": "tool_call", "data": {"name": name, "args": args}},
                [{"type": "tool_call", "name": name, "args": args}],
            )
        if self._delay_seconds:
            time.sleep(self._delay_seconds)
        yield (
            {"type": "message", "data": self._final_message},
            [{"type": "assistant", "content": self._final_message}],
        )
