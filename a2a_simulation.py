import requests
import uuid
import json


import asyncio
import httpx

# Official A2A Client Imports
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types.a2a_pb2 import Role, SendMessageRequest



def test_a2a_connection():
    # The URL where your LangChain A2A server is running
    url = "http://localhost:8000/" 
    
    # 1. Construct the standard A2A JSON-RPC payload
    payload = {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "role": "user",
                "parts": [{"kind": "text", "text": "What is the weather in Paris?"}],
                "messageId": str(uuid.uuid4())
            }
        }
    }
    
    print(f"📡 Sending A2A request to {url}...")
    
    try:
        # 2. Send the POST request
        response = requests.post(url, json=payload, headers={"Accept": "application/json"})
        response.raise_for_status() # Raise an error for bad HTTP status codes
        
        result = response.json()
        
        # 3. Extract the text from the A2A response
        extracted_text = "(No text found in response)"
        
        # Check 'artifacts' first (standard for completed tasks)
        for art in result.get("result", {}).get("artifacts", []) or []:
            for part in art.get("parts", []) or []:
                if part.get("kind") == "text" and part.get("text"):
                    extracted_text = part["text"]
                    break
        
        # Fallback to 'status.message' if no artifacts are present
        if extracted_text == "(No text found in response)":
            msg = (result.get("result", {}).get("status", {}) or {}).get("message", {}) or {}
            for part in msg.get("parts", []) or []:
                if part.get("kind") == "text" and part.get("text"):
                    extracted_text = part["text"]
                    break
        
        print("\n✅ SUCCESS: Communication is working!")
        print(f"Response from LangChain Agent:\n{extracted_text}")
        
        # Optional: Print raw JSON to see the exact structure returned
        # print("\n--- RAW RESPONSE ---")
        # print(json.dumps(result, indent=2))
        
    except requests.exceptions.ConnectionError:
        print("❌ Connection Error: Could not connect to the server.")
        print("   -> Is your LangChain A2A server running on port 8000?")
    except requests.exceptions.HTTPError as e:
        print(f"❌ HTTP Error: {e}")
        print(f"   -> Server response: {response.text}")
    except Exception as e:
        print(f"❌ An unexpected error occurred: {e}")




async def a2a_test():
    # 1. Fetch the Agent Card from the server (Discovery)
    print("Fetching Agent Card...")
    async with httpx.AsyncClient() as httpx_client:
        resolver = A2ACardResolver(
            httpx_client=httpx_client,
            base_url='http://127.0.0.1:9999',
        )
        public_agent_card = await resolver.get_agent_card()
        print(f"✅ Found agent: {public_agent_card.name}")

    # 2. Initialize a non-streaming client
    print('\nInitializing a non-streaming client...')
    config = ClientConfig(streaming=False)
    client = await create_client(agent=public_agent_card, client_config=config)

    # 3. Create and send the message
    text_query = 'What is the weather in Paris?'
    message = new_text_message(text_query, role=Role.ROLE_USER)
    request = SendMessageRequest(message=message)

    print(f'\nSending message: "{text_query}"')
    print('Response:')
    
    # 4. Iterate over the response chunks
    async for chunk in client.send_message(request):
        print(chunk)
        
    # Clean up the connection
    await client.close()


if __name__ == "__main__":
    # Ensure you have requests installed: pip install requests
    #test_a2a_connection()
    asyncio.run(a2a_test())
