import requests
import uuid
import json


import asyncio
import httpx

# Official A2A Client Imports
from a2a.client import A2ACardResolver, ClientConfig, create_client
from a2a.helpers import new_text_message
from a2a.types.a2a_pb2 import Role, SendMessageRequest



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
