# client_push.py
import httpx
import uuid
import asyncio
import threading
import uvicorn
from fastapi import FastAPI, Request
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest
# Note: Ensure PushNotificationConfig is available in your SDK version
try:
    from a2a.types.a2a_pb2 import PushNotificationConfig
except ImportError:
    from a2a.types import PushNotificationConfig 

from client_utils import BaseA2AClient

class PushNotificationA2AClient(BaseA2AClient):
    def __init__(self, server_url: str, webhook_port: int = 8080, local_ip: str = "192.168.0.107"):
        super().__init__(server_url)
        self.webhook_port = webhook_port
        self.local_ip = local_ip
        self.result_queue = asyncio.Queue()
        self.final_response = ""
        
        self.app = FastAPI()
        self._setup_webhook_routes()
        self._server_thread = None

    def _setup_webhook_routes(self):
        @self.app.post("/webhook")
        async def webhook_endpoint(request: Request):
            payload = await request.json()
            print(f"\n[WEBHOOK RECEIVED]: {payload}")
            
            extracted_text = self._extract_from_webhook(payload)
            if extracted_text:
                self.final_response += extracted_text + " "
                print(f"[WEBHOOK UPDATE]: {extracted_text}")
                
            if self._is_task_completed(payload):
                await self.result_queue.put("COMPLETED")
                
            # Standard JSON-RPC success response
            return {"jsonrpc": "2.0", "result": {"success": True}, "id": payload.get("id")}

    def _extract_from_webhook(self, payload: dict) -> str:
        """Extract text from the JSON-RPC push notification payload."""
        params = payload.get("params", {})
        task = params.get("task", {})
        
        # Extract from status message
        status = task.get("status", {})
        message = status.get("message", {})
        parts = message.get("parts", [])
        for part in parts:
            if "text" in part:
                return f"[STATUS]: {part['text']}"
                
        # Extract from artifacts
        artifacts = task.get("artifacts", [])
        for artifact in artifacts:
            parts = artifact.get("parts", [])
            for part in parts:
                if "text" in part:
                    return f"[ARTIFACT]: {part['text']}"
                    
        return None

    def _is_task_completed(self, payload: dict) -> bool:
        params = payload.get("params", {})
        task = params.get("task", {})
        status = task.get("status", {})
        state = status.get("state", "")
        return state in ["TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "completed", "failed"]

    def _start_webhook_server(self):
        config = uvicorn.Config(self.app, host="0.0.0.0", port=self.webhook_port, log_level="warning")
        server = uvicorn.Server(config)
        # Run in a daemon thread so it doesn't block the main asyncio loop
        self._server_thread = threading.Thread(target=server.run, daemon=True)
        self._server_thread.start()
        print(f"Webhook server started on http://{self.local_ip}:{self.webhook_port}/webhook")

    async def initialize(self):
        self._start_webhook_server()
        await asyncio.sleep(1) # Give server a moment to bind
        
        self.httpx_client = httpx.AsyncClient(timeout=300.0)
        resolver = A2ACardResolver(httpx_client=self.httpx_client, base_url=self.server_url)
        public_agent_card = await resolver.get_agent_card()
        
        # For push notifications, the initial HTTP request doesn't stream
        config = ClientConfig(streaming=False, httpx_client=self.httpx_client)
        self.client = await create_client(agent=public_agent_card, client_config=config)

    async def execute_task(self, text: str, context_id: Optional[str] = None) -> Tuple[str, str]:
        if not context_id:
            context_id = str(uuid.uuid4())
            print(f">>> CLIENT GENERATED CONTEXT ID: {context_id}")

        part = Part(text=text)
        webhook_url = f"http://{self.local_ip}:{self.webhook_port}/webhook"
        push_config = PushNotificationConfig(url=webhook_url)
        
        message = Message(
            role=Role.ROLE_USER,
            parts=[part],
            message_id=str(uuid.uuid4()),
            context_id=context_id
        )
        
        # Note: Depending on your exact A2A SDK version, the field name might vary 
        # (e.g., push_notification_config vs pushNotificationConfig)
        request = SendMessageRequest(
            message=message,
            push_notification_config=push_config 
        )

        print(f"\n[CLIENT SENDING]: {text}")
        print(f"--- WAITING FOR SERVER RESPONSE (PUSH to {webhook_url}) ---")
        
        # Send initial request (yields initial task ID/acknowledgment)
        async for chunk in self.client.send_message(request):
            pass 
            
        print("--- WAITING FOR WEBHOOK COMPLETION ---")
        try:
            await asyncio.wait_for(self.result_queue.get(), timeout=300.0)
        except asyncio.TimeoutError:
            print("Timeout waiting for webhook completion!")
            
        return context_id, self.final_response.strip()

    async def close(self):
        if hasattr(self, 'httpx_client'):
            await self.httpx_client.aclose()
        await super().close()