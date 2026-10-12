from __future__ import annotations

from urllib.parse import quote
from uuid import uuid4

import aiohttp
from yarl import URL


class BlueBubblesClient:
    """REST sends have no automatic retries; a timeout can mean an uncertain send."""

    def __init__(self, server_url: str, password: str) -> None:
        url = URL(server_url)
        if (
            url.scheme not in ("http", "https")
            or not url.host
            or url.user
            or url.password
            or url.query_string
            or url.fragment
            or url.path not in ("", "/")
        ):
            raise ValueError("BlueBubbles requires a root server URL without credentials or query")
        if url.scheme == "http" and url.host not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Use HTTPS for a remote BlueBubbles server")
        self.server_url = str(url).rstrip("/")
        self._password = password
        self._session: aiohttp.ClientSession | None = None
        self._closed = False

    @property
    def socket_url(self) -> str:
        # BlueBubbles applies decodeURI after Socket.IO parses the query string.
        # Escape percent itself for that second decode, but retain reserved characters.
        password = self._password.replace("%", "%25")
        return f"{self.server_url}?password={quote(password, safe='')}"

    async def request(self, method: str, path: str, **kwargs: object) -> dict:
        if self._closed:
            raise RuntimeError("BlueBubbles client is closed")
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60))
        try:
            async with self._session.request(
                method,
                f"{self.server_url}{path}",
                params={"password": self._password},
                allow_redirects=False,
                **kwargs,
            ) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"BlueBubbles request failed (HTTP {response.status})")
                result = await response.json()
                if not isinstance(result, dict) or result.get("status") != 200:
                    raise RuntimeError("BlueBubbles returned an invalid response")
                return result
        except (aiohttp.ClientError, TimeoutError, ValueError):
            # aiohttp exception strings may include the password-bearing request URL.
            raise RuntimeError("BlueBubbles request failed; delivery may be uncertain") from None

    async def check_connection(self) -> None:
        await self.request("GET", "/api/v1/ping")

    async def send_text(self, chat_guid: str, content: str) -> str:
        result = await self.request(
            "POST",
            "/api/v1/message/text",
            json={
                "chatGuid": chat_guid,
                "message": content,
                "method": "apple-script",
                "tempGuid": str(uuid4()),
            },
        )
        data = result.get("data")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("guid"), str)
            or not data["guid"]
            or data.get("error") not in (None, 0)
        ):
            raise RuntimeError("BlueBubbles did not return a successful message receipt")
        return data["guid"]

    async def close(self) -> None:
        self._closed = True
        if self._session:
            await self._session.close()
