# client_streaming.py
import httpx
import uuid
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.types.a2a_pb2 import Message, Part, Role, SendMessageRequest
from google.protobuf.json_format import MessageToDict
from client_utils import BaseA2AClient, extract_text_heuristically

class StreamingA2AClient(BaseA2AClient):
    async def initialize(self):
        self.httpx_client = httpx.AsyncClient(timeout=300.0)
        resolver = A2ACardResolver(httpx_client=self.httpx_client, base_url=self.server_url)
        public_agent_card = await resolver.get_agent_card()
        print(f"Found agent: {public_agent_card.name}")
        
        config = ClientConfig(streaming=True, httpx_client=self.httpx_client)
        self.client = await create_client(agent=public_agent_card, client_config=config)

    async def execute_task(self, text: str, context_id: Optional[str] = None) -> Tuple[str, str]:
        if not context_id:
            context_id = str(uuid.uuid4())
            print(f">>> CLIENT GENERATED CONTEXT ID: {context_id}")

        part = Part(text=text)
        message = Message(
            role=Role.ROLE_USER,
            parts=[part],
            message_id=str(uuid.uuid4()),
            context_id=context_id
        )
        request = SendMessageRequest(message=message)

        print(f"\n[CLIENT SENDING]: {text}")
        print("--- WAITING FOR SERVER RESPONSE (STREAMING) ---")
        
        full_response_text = ""
        agent_response = ""
        
        async for chunk in self.client.send_message(request):
            converted = MessageToDict(chunk, preserving_proto_field_name=True)
            extracted_text = extract_text_heuristically(converted)
            
            if "artifact_update" in converted:
                agent_response = extracted_text
            elif extracted_text:
                full_response_text += extracted_text + " "
                print(extracted_text)
                
        # Prefer artifact if available, otherwise return concatenated status updates
        final_text = agent_response.strip() if agent_response else full_response_text.strip()
        return context_id, final_text

    async def close(self):
        if hasattr(self, 'httpx_client'):
            await self.httpx_client.aclose()
        await super().close()