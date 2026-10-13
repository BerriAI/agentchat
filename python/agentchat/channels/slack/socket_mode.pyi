# Public compatibility signatures; update with the corresponding Rust binding API.
from slack_sdk.web.async_client import AsyncWebClient

from agentchat.channels.slack.transport import SlackEventReceiver

EVENT_DRAIN_TIMEOUT: int

class SocketMode:
    def __init__(
        self,
        *,
        app_token: str,
        web_client: AsyncWebClient | None = ...,
        max_pending_events: int = ...,
    ) -> None: ...
    async def run(self, receiver: SlackEventReceiver) -> None: ...
    async def close(self) -> None: ...
    async def is_connected(self) -> bool: ...
