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
"""

from typing import Optional, List, Dict, Any, Iterator
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
    
    def __init__(self, delay_seconds: float = 0.0):
        """Initialize with empty tool call list.

        Args:
            delay_seconds: Optional per-event delay to simulate real LLM
                streaming latency (used by the demo app, not by tests).
        """
        self._tool_calls: List[Dict[str, Any]] = []
        self._final_message: str = "This is a mock agent response."
        self._delay_seconds: float = delay_seconds
    
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
        
        Args:
            message: User input message (ignored in mock)
            thread_id: Session identifier (ignored in mock)
            
        Yields:
            Event dictionaries:
            - {"type": "tool_call", "data": {"name": ..., "args": ...}}
            - {"type": "message", "data": "final response text"}
        """
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
