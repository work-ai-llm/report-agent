"""Interfaces and protocols for Streamlit UI dependency injection.

This module defines the abstract contracts (Protocols) that allow the Streamlit UI
to interact with external dependencies (database, agent, file storage) without
being tightly coupled to specific implementations. This enables easy testing with mocks.

## Data Flow Overview

The Streamlit UI depends on three core services:
1. **Checkpointer**: Persists session state (conversation history) per thread_id
2. **FileStore**: Global virtual filesystem for findings and reports
3. **AgentRunner**: Executes the pen-test agent and streams events back

Each Protocol defines the minimum interface required. Production implementations
use PostgreSQL, while test implementations use in-memory dictionaries.
"""

from typing import Optional, List, Dict, Any, Iterator, Protocol


class CheckpointerProtocol(Protocol):
    """Protocol for session persistence (conversation history).
    
    This interface abstracts the database layer that stores conversation history
    for each thread_id (session). The Streamlit UI uses this to retrieve past
    messages when a user selects an existing session.
    
    ## Usage Pattern
    ```python
    # Save conversation state after each message
    checkpointer.put(thread_id="session_123", state=conversation_state)
    
    # Load conversation when user switches sessions
    state = checkpointer.get(thread_id="session_123")
    ```
    """
    
    def get(self, thread_id: str) -> Optional[Dict[str, Any]]:
        """Retrieve conversation state for a given thread.
        
        Args:
            thread_id: Unique identifier for the session/thread
            
        Returns:
            Saved state dictionary or None if not found
        """
        ...
    
    def put(self, thread_id: str, state: Dict[str, Any]) -> None:
        """Save conversation state for a given thread.
        
        Args:
            thread_id: Unique identifier for the session/thread
            state: Conversation state to persist
        """
        ...
    
    def list_threads(self) -> List[str]:
        """List all available thread IDs.
        
        Returns:
            List of thread_id strings for session selector dropdown
        """
        ...
    
    def delete(self, thread_id: str) -> bool:
        """Delete a thread and its persisted conversation state.
        
        Args:
            thread_id: Unique identifier for the session/thread to delete
            
        Returns:
            True if the thread existed and was deleted, False otherwise
        """
        ...


class FileStoreProtocol(Protocol):
    """Protocol for global virtual filesystem.
    
    This interface abstracts the storage layer for files uploaded as findings
    and generated reports. Files are stored globally (shared across all sessions)
    but can be organized by namespace if needed.
    
    ## Data Flow
    1. User uploads finding → UI calls `write_file("findings/nmap.txt", content)`
    2. Agent reads finding → Agent calls `read_file("findings/nmap.txt")`
    3. Agent generates report → Agent calls `write_file("reports/report.md", content)`
    4. User downloads report → UI calls `read_file("reports/report.md")`
    
    ## Namespace Convention
    - `/findings/` - Uploaded reconnaissance data (global, shared)
    - `/reports/` - Generated penetration test reports (global, shared)
    """
    
    def read_file(self, path: str) -> Optional[bytes]:
        """Read file content from virtual filesystem.
        
        Args:
            path: File path (e.g., "findings/nmap.txt")
            
        Returns:
            File content as bytes or None if not found
        """
        ...
    
    def write_file(self, path: str, content: bytes) -> None:
        """Write file content to virtual filesystem.
        
        Args:
            path: File path (e.g., "findings/nmap.txt")
            content: File content as bytes
        """
        ...
    
    def list_files(self, prefix: str = "") -> List[str]:
        """List files matching optional prefix.
        
        Args:
            prefix: Optional path prefix to filter results (e.g., "findings/")
            
        Returns:
            List of file paths matching the prefix
        """
        ...
    
    def delete_file(self, path: str) -> bool:
        """Delete file from virtual filesystem.
        
        Args:
            path: File path to delete
            
        Returns:
            True if deleted, False if not found
        """
        ...


class AgentRunnerProtocol(Protocol):
    """Protocol for agent execution with streaming events.
    
    This interface abstracts the LangGraph agent execution, allowing the UI
    to stream tool calls and intermediate steps in real-time without blocking.
    
    ## Data Flow
    1. User sends message → UI calls `run_agent(message, thread_id)`
    2. Agent processes → Yields events (tool_call, tool_result, message)
    3. UI renders → Displays each event as it arrives
    
    ## Event Types
    - `tool_call`: Agent is invoking a tool (shows tool name + arguments)
    - `tool_result`: Tool execution completed (shows result preview)
    - `message`: Final agent response to display to user
    """
    
    def run(self, message: str, thread_id: str) -> Iterator[Dict[str, Any]]:
        """Execute agent with streaming event output.
        
        Args:
            message: User input message
            thread_id: Session identifier for context retrieval
            
        Yields:
            Event dictionaries with structure:
            - type: "tool_call" | "tool_result" | "message"
            - data: Event-specific payload
            
        Example:
            ```python
            for event in agent.run("Scan target", thread_id="session_1"):
                if event["type"] == "tool_call":
                    show_tool_call(event["data"])
                elif event["type"] == "message":
                    show_response(event["data"])
            ```
        """
        ...
