"""
Mock objects for testing Streamlit UI without external dependencies.

This module provides in-memory mock implementations of:
- MockStore: Simulates PostgresStore for virtual filesystem
- MockCheckpointer: Simulates PostgresSaver for session persistence  
- MockAgent: Simulates LangChain agent with pre-programmed responses

All mocks are designed to be injected into PenTestAgentService for testing.
"""

import asyncio
from typing import Optional, Dict, Any, List, AsyncIterator
from dataclasses import dataclass, field

from langchain_core.messages import AIMessage, ToolMessage


@dataclass
class MockStoreItem:
    """Represents an item stored in the mock store."""
    namespace: tuple
    key: str
    value: Any


class MockStore:
    """
    In-memory mock of PostgresStore for virtual filesystem operations.
    
    Supports:
    - aput(): Store files in global or session-specific namespaces
    - aget(): Retrieve files by namespace and key
    - asearch(): Search files by prefix
    
    Thread-safe with asyncio lock for concurrent test scenarios.
    """
    
    def __init__(self):
        self._storage: Dict[str, MockStoreItem] = {}
        self._lock = asyncio.Lock()
        
    def _make_key(self, namespace: tuple, key: str) -> str:
        """Create unique storage key from namespace and key."""
        return f"{':'.join(namespace)}::{key}"
        
    async def aput(self, namespace: tuple, key: str, value: Any) -> None:
        """Store an item asynchronously."""
        async with self._lock:
            storage_key = self._make_key(namespace, key)
            self._storage[storage_key] = MockStoreItem(
                namespace=namespace,
                key=key,
                value=value
            )
            
    async def aget(self, namespace: tuple, key: str) -> Optional[MockStoreItem]:
        """Retrieve an item asynchronously."""
        async with self._lock:
            storage_key = self._make_key(namespace, key)
            return self._storage.get(storage_key)
            
    async def asearch(
        self, 
        namespace: tuple, 
        prefix: str = ""
    ) -> AsyncIterator[MockStoreItem]:
        """Search items by namespace and optional prefix."""
        async with self._lock:
            ns_prefix = ':'.join(namespace) + "::"
            full_prefix = ns_prefix + prefix
            
            for item in self._storage.values():
                storage_key = self._make_key(item.namespace, item.key)
                if storage_key.startswith(full_prefix):
                    yield item
                    
    async def clear(self) -> None:
        """Clear all stored items. Useful for test isolation."""
        async with self._lock:
            self._storage.clear()


@dataclass
class MockCheckpoint:
    """Represents a checkpoint in the mock checkpointer."""
    thread_id: str
    messages: List[Dict[str, Any]] = field(default_factory=list)


class MockCheckpointer:
    """
    In-memory mock of PostgresSaver for session persistence.
    
    Supports:
    - aget(): Retrieve checkpoint for a thread
    - aput(): Save checkpoint for a thread (simplified)
    
    Note: This is a simplified mock that only stores messages,
    not full LangGraph checkpoint structure.
    """
    
    def __init__(self):
        self._checkpoints: Dict[str, MockCheckpoint] = {}
        self._lock = asyncio.Lock()
        
    async def aget(self, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Get checkpoint for thread_id from config."""
        async with self._lock:
            thread_id = config.get('configurable', {}).get('thread_id')
            if not thread_id:
                return None
                
            checkpoint = self._checkpoints.get(thread_id)
            if checkpoint:
                return {
                    'channel_values': {
                        'messages': checkpoint.messages
                    }
                }
            return None
            
    async def aput(
        self, 
        config: Dict[str, Any], 
        messages: List[Dict[str, Any]]
    ) -> None:
        """Save messages for thread_id from config."""
        async with self._lock:
            thread_id = config.get('configurable', {}).get('thread_id')
            if thread_id:
                self._checkpoints[thread_id] = MockCheckpoint(
                    thread_id=thread_id,
                    messages=messages
                )
                
    async def clear(self) -> None:
        """Clear all checkpoints. Useful for test isolation."""
        async with self._lock:
            self._checkpoints.clear()


class MockAgent:
    """
    In-memory mock of LangChain agent for testing agent execution flow.
    
    Supports:
    - astream(): Stream pre-programmed responses and tool calls
    - configure_response(): Set custom response sequence
    
    The mock yields events in the same format as real agent:
    {'node_name': {'messages': [AIMessage or ToolMessage]}}
    """
    
    def __init__(self):
        self._response_sequence: List[Dict[str, Any]] = []
        self._call_history: List[str] = []
        
    def configure_response(
        self, 
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        final_message: str = "Task completed successfully.",
        tool_results: Optional[List[str]] = None
    ) -> None:
        """
        Configure the mock agent's response sequence.
        
        Args:
            tool_calls: List of tool call dicts with 'name' and 'args'
            final_message: Final AI message after tool calls
            tool_results: Optional list of tool result strings
        """
        self._response_sequence = []
        
        # Add tool calls first
        if tool_calls:
            for i, tc in enumerate(tool_calls):
                # Ensure tool call has required 'id' field
                tool_call_with_id = tc.copy()
                if 'id' not in tool_call_with_id:
                    tool_call_with_id['id'] = f"mock_call_{i}"
                    
                # Create real AIMessage with tool_calls
                ai_msg = AIMessage(
                    content='',
                    tool_calls=[tool_call_with_id]
                )
                self._response_sequence.append({
                    'agent': {'messages': [ai_msg]}
                })
                
                # Add corresponding tool result if provided
                if tool_results and len(tool_results) > 0:
                    tool_msg = ToolMessage(
                        content=tool_results.pop(0),
                        status='success',
                        tool_call_id=f"mock_call_{i}"
                    )
                    self._response_sequence.append({
                        'tools': {'messages': [tool_msg]}
                    })
                    
        # Add final message
        if final_message:
            ai_msg = AIMessage(
                content=final_message,
                tool_calls=[]
            )
            self._response_sequence.append({
                'agent': {'messages': [ai_msg]}
            })
            
    async def astream(
        self, 
        input_data: Dict[str, Any],
        config: Optional[Dict[str, Any]] = None,
        stream_mode: str = "updates"
    ) -> AsyncIterator[Dict[str, Any]]:
        """
        Stream configured response sequence.
        
        Records the input query in call_history for verification.
        """
        # Record the call
        messages = input_data.get('messages', [])
        if messages:
            query = messages[0].get('content', '')
            self._call_history.append(query)
            
        # Yield configured sequence
        for chunk in self._response_sequence:
            await asyncio.sleep(0.01)  # Small delay to simulate streaming
            yield chunk
            
    def get_call_history(self) -> List[str]:
        """Get list of all queries sent to the mock agent."""
        return self._call_history.copy()
        
    def clear_history(self) -> None:
        """Clear call history. Useful for test isolation."""
        self._call_history.clear()


def create_mock_service_components(
    preloaded_findings: Optional[Dict[str, str]] = None,
    preloaded_reports: Optional[Dict[str, str]] = None,
    default_thread_id: str = "test_session"
):
    """
    Factory function to create a complete set of mock components.
    
    Pre-populates store with findings and reports for testing.
    Uses synchronous operations internally to avoid nested event loop issues.
    
    Args:
        preloaded_findings: Dict mapping filename -> content
        preloaded_reports: Dict mapping filename -> content
        default_thread_id: Default thread ID for reports
        
    Returns:
        Tuple of (MockStore, MockCheckpointer, MockAgent)
    """
    store = MockStore()
    checkpointer = MockCheckpointer()
    agent = MockAgent()
    
    # Pre-load findings (global namespace) - sync version
    if preloaded_findings:
        for filename, content in preloaded_findings.items():
            # Direct storage without asyncio for test simplicity
            storage_key = f"filesystem::findings/{filename}"
            store._storage[storage_key] = MockStoreItem(
                namespace=("filesystem",),
                key=f"findings/{filename}",
                value={"content": content}
            )
            
    # Pre-load reports (session-specific namespace) - sync version
    if preloaded_reports:
        for filename, content in preloaded_reports.items():
            storage_key = f"filesystem::reports/{default_thread_id}/{filename}"
            store._storage[storage_key] = MockStoreItem(
                namespace=("filesystem",),
                key=f"reports/{default_thread_id}/{filename}",
                value={"content": content}
            )
            
    return store, checkpointer, agent
