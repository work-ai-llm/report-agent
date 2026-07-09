# tool description
# initial way to confirm rough api working or not (used to discover that api didn't work without vpn)
# no further use

import os
import httpx
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("MISTRAL_API_KEY")

# The exact payload LangChain sends
payload = {
    "model": "mistral-large-2512",
    "messages": [{"role": "user", "content": "Tell me about yourself in one short paragraph."}],
    "temperature": 0.2,
    "stream": False
}

headers = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json"
}

print("Sending raw request to Mistral API...")
try:
    # Hardcoded 30-second timeout. No retries, no LangChain magic.
    with httpx.Client(timeout=30.0) as client:
        response = client.post("https://api.mistral.ai/v1/chat/completions", json=payload, headers=headers)
        response.raise_for_status()
        print("✅ SUCCESS!")
        print(response.json()["choices"][0]["message"]["content"])
        
except httpx.HTTPStatusError as e:
    print(f"❌ HTTP ERROR: {e.response.status_code} - {e.response.text}")
except httpx.ReadTimeout:
    print("❌ RAW REQUEST TIMED OUT. This means your network/firewall is blocking the connection, or the API key is invalid/suspended.")
except Exception as e:
    print(f"❌ UNEXPECTED ERROR: {e}")