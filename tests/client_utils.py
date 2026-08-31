# client_utils.py
import uuid
from typing import Optional, Tuple

class BaseA2AClient:
    """Interface for A2A clients. Tests should only interact with this interface."""
    def __init__(self, server_url: str):
        self.server_url = server_url
        self.client = None

    async def initialize(self):
        """Setup connection, resolve agent card, etc."""
        raise NotImplementedError

    async def execute_task(self, text: str, context_id: Optional[str] = None) -> Tuple[str, str]:
        """
        Sends a task and waits for completion.
        Returns: (context_id, final_response_text)
        """
        raise NotImplementedError

    async def close(self):
        """Cleanup resources."""
        if self.client:
            await self.client.close()

def extract_text_heuristically(chunk_dict: dict) -> Optional[str]:
    """Recursively search any dictionary/object for textual data (Used by Streaming)."""
    if "status_update" in chunk_dict:
        status = chunk_dict["status_update"].get("status", {})
        message = status.get("message", {})
        parts = message.get("parts", [])
        for part in parts:
            if "text" in part:
                return f"[STATUS]: {part['text']}"

    elif "artifact_update" in chunk_dict:
        artifact = chunk_dict["artifact_update"].get("artifact", {})
        parts = artifact.get("parts", [])
        for part in parts:
            if "text" in part:
                return f"[ARTIFACT]: {part['text']}"   
                
    elif 'task' in chunk_dict:
        task_dict = chunk_dict['task']
        task_id = task_dict.get('id')
        status = task_dict.get("status", {}).get("state")
        history_list = task_dict.get("history", [])
        task_text = None
        
        for history_item in history_list:
            parts = history_item.get("parts", [])
            for part in parts:
                if "text" in part:
                    task_text = part['text']
                    
        return f"[TASK STATE]: {status}\nTask Id: {task_id}\nText: {task_text}"
        
    return None