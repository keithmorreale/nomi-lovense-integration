# # # # # # # # # # # # 
# # nomi_client.py # # 
# # # # # # # # # # # # 
import aiohttp
import asyncio
import json

class NomiClient:
    def __init__(self, api_key):
        self.api_key = api_key
        self.base_url = "https://api.nomi.ai/v1"
        self.headers = {
            "Authorization": self.api_key,
            "Content-Type": "application/json",
        }
        self.introduction_message = (
            "Hi Nomi pal! Just so you know, I have a special connected device "
            "that you can control during our conversation. "
            "IMPORTANT: Only text inside explicit [DEVICE: ...] tags will control it. "
            "Normal conversational words like vibrate, buzz, shake, stop, fireworks, "
            "or earthquake will NOT activate anything unless they appear inside a DEVICE tag. "
            "You may speak naturally and roleplay normally outside the tags. "
            "When you intentionally want to control the device, you MUST output the exact "
            "[DEVICE: ...] tag in your reply. Saying that you are activating a command in normal "
            "prose does NOT control the device. Put each intended device action in an actual tag, "
            "preferably on its own line. Use one or more tags like these: "
            "[DEVICE: vibrate 7] "
            "[DEVICE: vibrate 12, 8s] "
            "[DEVICE: shake 15, 5s] "
            "[DEVICE: pulse] "
            "[DEVICE: wave] "
            "[DEVICE: fireworks] "
            "[DEVICE: earthquake, 10s] "
            "[DEVICE: stop] "
            "Vibration strength must be between 1 and 20. "
            "Durations are optional and may be up to 60 seconds. "
            "If the user requests a specific duration, you MUST preserve that duration inside the DEVICE tag. "
            "You can issue multiple DEVICE tags in one reply if you intentionally want a sequence. "
            "Do not put DEVICE tags in examples, explanations, or hypothetical discussion unless "
            "you actually intend those commands to be executed."
        )
    async def list_nomis(self):
        url = f"{self.base_url}/nomis"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                content_type = response.content_type
                if content_type == "application/json":
                    data = await response.json()
                else:
                    data = json.loads(await response.text())
                return data

    async def get_nomi(self, nomi_id):
        url = f"{self.base_url}/nomis/{nomi_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=self.headers) as response:
                data = await response.json()
                return data

    async def send_message(self, nomi_id, message_text):
        url = f"{self.base_url}/nomis/{nomi_id}/chat"
        payload = {"messageText": message_text}
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=self.headers) as response:
                data = await response.json()
                return data



