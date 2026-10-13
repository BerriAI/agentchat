from typing import Any

import aiohttp

API_ROOT: str

class DiscordRESTClient:
    _session: aiohttp.ClientSession | None
    def __init__(self, bot_token: str) -> None: ...
    async def send_text(self, channel_id: int, payload: dict[str, Any]) -> int: ...
    async def close(self) -> None: ...
