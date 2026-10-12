from __future__ import annotations

import aiohttp

API_ROOT = "https://discord.com/api/v10"


class DiscordRESTClient:
    """One-attempt message sends; discord.py's gateway owns reconnects separately."""

    def __init__(self, bot_token: str) -> None:
        self._token = bot_token
        self._session: aiohttp.ClientSession | None = None
        self._closed = False

    async def send_text(self, channel_id: int, payload: dict) -> int:
        if self._closed:
            raise RuntimeError("Discord send client is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        try:
            async with self._session.post(
                f"{API_ROOT}/channels/{channel_id}/messages",
                headers={"Authorization": f"Bot {self._token}"},
                json=payload,
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"Discord send failed (HTTP {response.status})")
                result = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise RuntimeError("Discord request failed; delivery may be uncertain") from None
        message_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(message_id, str) or not message_id.isascii() or not message_id.isdigit():
            raise RuntimeError("Discord did not return a message receipt")
        if int(message_id) <= 0:
            raise RuntimeError("Discord did not return a message receipt")
        return int(message_id)

    async def close(self) -> None:
        self._closed = True
        if self._session:
            await self._session.close()
