"""
PenTestAgentService - Wrapper for report_agent with dependency injection support.

This service class wraps the global report_agent module and provides:
1. Production mode: Uses real PostgresStore, PostgresSaver, and langchain_agent
2. Mock mode: Accepts injected mock objects for testing without external dependencies

The service manages:
- Session selection (thread_id for conversation history)
- Virtual filesystem operations (upload/download findings and reports)
- Agent execution with streaming tool call events
"""

import asyncio
from typing import Optional, AsyncGenerator, Dict, Any, List
from dataclasses import dataclass


@dataclass
class ToolCallEvent:
    """Represents a tool call event streamed from agent execution."""
    tool_name: str
    args: Dict[str, Any]
    preview_args: Optional[Dict[str, Any]] = None


@dataclass 
class AgentMessageEvent:
    """Represents an agent message event."""
    content: str
    message_type: str  # 'ai', 'tool_result', 'user'


class PenTestAgentService:
    """
    Service wrapper for the pentest report agent.
    
    In production, this imports and uses the real report_agent module.
    In testing, this accepts mock objects via constructor injection.
    
    Attributes:
        checkpointer: Session persistence store (PostgresSaver or mock)
        store: Virtual filesystem store (PostgresStore or mock)
        agent: LangChain agent instance (real or mock)
        current_thread_id: Currently selected session/thread ID
    """
    
    def __init__(
        self,
        checkpointer: Optional[Any] = None,
        store: Optional[Any] = None,
        agent: Optional[Any] = None,
        thread_id: Optional[str] = "default"
    ):
        """
        Initialize the service.
        
        Args:
            checkpointer: Session checkpointer. If None, will use real PostgresSaver.
            store: Virtual filesystem store. If None, will use real PostgresStore.
            agent: LangChain agent. If None, will import from report_agent module.
            thread_id: Initial thread/session ID.
        """
        self._checkpointer = checkpointer
        self._store = store
        self._agent = agent
        self._current_thread_id = thread_id
        self._is_mock_mode = any([checkpointer, store, agent])
        
    @property
    def is_mock_mode(self) -> bool:
        """Returns True if using injected mock objects."""
        return self._is_mock_mode
    
    @property
    def current_thread_id(self) -> str:
        """Get current session/thread ID."""
        return self._current_thread_id
    
    def set_thread_id(self, thread_id: str) -> None:
        """Switch to a different session/thread."""
        self._current_thread_id = thread_id
        
    async def initialize(self) -> None:
        """
        Initialize real agent connections if not in mock mode.
        
        In production, this calls report_agent.initiate_connection().
        In mock mode, this is a no-op since objects are already provided.
        """
        if not self._is_mock_mode:
            from agent import report_agent
            await report_agent.initiate_connection()
            self._checkpointer = report_agent.checkpointer
            self._store = report_agent.store
            self._agent = report_agent.langchain_agent
            
    async def upload_finding(self, filename: str, content: str) -> bool:
        """
        Upload a findings file to the virtual filesystem.
        
        Files are stored in the global namespace under /findings/
        making them accessible across all sessions.
        
        Args:
            filename: Name of the findings file.
            content: File content as string.
            
        Returns:
            True if upload successful, False otherwise.
        """
        if self._store is None:
            return False
            
        namespace = ("filesystem",)
        key = f"findings/{filename}"
        
        try:
            await self._store.aput(namespace, key, {"content": content})
            return True
        except Exception as e:
            print(f"Upload failed: {e}")
            return False
            
    async def list_findings(self) -> List[str]:
        """
        List all uploaded findings files.
        
        Returns:
            List of filenames in the /findings/ directory.
        """
        if self._store is None:
            return []
            
        namespace = ("filesystem",)
        
        try:
            # Search for files under findings/ prefix
            results = []
            async for item in self._store.asearch(namespace, prefix="findings/"):
                # Extract filename from key
                key = item.key
                if key.startswith("findings/"):
                    filename = key[len("findings/"):].split("/")[-1]
                    results.append(filename)
            return results
        except Exception as e:
            print(f"List findings failed: {e}")
            return []
            
    async def download_report(self, filename: str) -> Optional[str]:
        """
        Download a report file from the virtual filesystem.
        
        Reports are stored per-session under /reports/{thread_id}/
        
        Args:
            filename: Name of the report file.
            
        Returns:
            File content as string, or None if not found.
        """
        if self._store is None:
            return None
            
        namespace = ("filesystem",)
        key = f"reports/{self._current_thread_id}/{filename}"
        
        try:
            item = await self._store.aget(namespace, key)
            if item:
                content = item.value
                if isinstance(content, dict):
                    return content.get("content", str(content))
                return str(content)
            return None
        except Exception as e:
            print(f"Download failed: {e}")
            return None
            
    async def list_reports(self) -> List[str]:
        """
        List all reports for the current session.
        
        Returns:
            List of report filenames for current thread_id.
        """
        if self._store is None:
            return []
            
        namespace = ("filesystem",)
        prefix = f"reports/{self._current_thread_id}/"
        
        try:
            results = []
            async for item in self._store.asearch(namespace, prefix=prefix):
                key = item.key
                if key.startswith(prefix):
                    filename = key[len(prefix):]
                    results.append(filename)
            return results
        except Exception as e:
            print(f"List reports failed: {e}")
            return []
            
    async def execute_agent(
        self, 
        user_message: str
    ) -> AsyncGenerator[tuple[str, Any], None]:
        """
        Execute the agent with a user message and stream events.
        
        Yields:
            Tuple of (event_type, event_data) where:
            - ('tool_call', ToolCallEvent) - Agent calling a tool
            - ('message', AgentMessageEvent) - Agent response or tool result
            - ('complete', str) - Final response text
            
        Args:
            user_message: The user's input message.
            
        Yields:
            Streaming events from agent execution.
        """
        if self._agent is None:
            yield ('error', "Agent not initialized")
            return
            
        config = {
            "configurable": {"thread_id": self._current_thread_id},
            "recursion_limit": 40,
        }
        
        query = f"[Streamlit UI]: {user_message}"
        
        try:
            async for chunk in self._agent.astream(
                {"messages": [{"role": "user", "content": query}]},
                config=config,
                stream_mode="updates"
            ):
                # Process each chunk from the agent
                for node_name, node_output in chunk.items():
                    if 'messages' in node_output:
                        for msg in node_output['messages']:
                            from langchain_core.messages import AIMessage, ToolMessage
                            
                            # Tool call from AI
                            if isinstance(msg, AIMessage):
                                if msg.tool_calls:
                                    for tc in msg.tool_calls:
                                        tool_name = tc['name']
                                        args = tc.get('args', {})
                                        
                                        # Create preview for large content
                                        preview_args = args.copy()
                                        if tool_name == 'write_file' and 'content' in preview_args:
                                            preview_content = preview_args['content'][:100].replace('\\n', ' ')
                                            preview_args['content'] = f"{preview_content}..."
                                            
                                        event = ToolCallEvent(
                                            tool_name=tool_name,
                                            args=args,
                                            preview_args=preview_args
                                        )
                                        yield ('tool_call', event)
                                        
                                elif msg.content:
                                    yield ('message', AgentMessageEvent(
                                        content=msg.content,
                                        message_type='ai'
                                    ))
                                    
                            # Tool execution result
                            elif isinstance(msg, ToolMessage):
                                yield ('message', AgentMessageEvent(
                                    content=f"Tool result: {msg.status}",
                                    message_type='tool_result'
                                ))
                                
        except Exception as e:
            yield ('error', str(e))
            
    async def get_session_history(self) -> List[Dict[str, Any]]:
        """
        Retrieve conversation history for current session.
        
        Returns:
            List of message dictionaries with role and content.
        """
        if self._checkpointer is None:
            return []
            
        try:
            # Get checkpoint for current thread
            config = {"configurable": {"thread_id": self._current_thread_id}}
            checkpoint = await self._checkpointer.aget(config)
            
            if checkpoint and 'channel_values' in checkpoint:
                messages = checkpoint['channel_values'].get('messages', [])
                return messages
            return []
        except Exception as e:
            print(f"Get history failed: {e}")
            return []
