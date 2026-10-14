import os
import httpx
from dotenv import load_dotenv

load_dotenv()
url = os.getenv("EVOLUTION_URL")
api_key = os.getenv("EVOLUTION_API_KEY")
instance = os.getenv("EVOLUTION_INSTANCE")
group = os.getenv("EVOLUTION_GROUP_JID")

headers = {
    "apikey": api_key,
    "Content-Type": "application/json"
}

body = {
    "where": {
        "key.remoteJid": group,
    },
    "limit": 5,
    "page": 1
}

with httpx.Client() as client:
    res = client.post(f"{url}/chat/findMessages/{instance}", json=body, headers=headers)
    print("Status Code:", res.status_code)
    if res.status_code == 200:
        data = res.json()
        if data.get("messages", {}).get("records"):
            print("First record keys:", data["messages"]["records"][0].keys())
            print("First record:", data["messages"]["records"][0])
        else:
            print("No records found.", data)
    else:
        print(res.text)
