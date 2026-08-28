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
from streamlit_ui.services.interfaces import (
    CheckpointerProtocol,
    FileStoreProtocol,
    AgentRunnerProtocol,
)


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
        files = self._file_store.list_files(prefix="findings/")
        # Strip prefix for cleaner display
        return [f.replace("findings/", "") for f in files]
    
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
        # Delegate to agent runner with current thread_id
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
        # Strip prefix for cleaner display
        return [f.replace("reports/", "") for f in files]
    
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
        return state.get("messages", [])
    
    def save_chat_history(self, messages: List[Dict[str, Any]]) -> None:
        """Persist conversation history for current thread.
        
        Args:
            messages: List of message dictionaries to save
            
        Data Flow:
            After agent response → save_chat_history() → checkpointer.put()
        """
        state = {"messages": messages}
        self._checkpointer.put(self._thread_id, state)
    
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
