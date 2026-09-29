from __future__ import annotations

import asyncio
import os
import re
from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Protocol, cast

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from agentchat.channels.base import MessageReceiver
from agentchat.models import Message, Sender

MENTION_PATTERN = re.compile(r"<@[^>]+>\s*")


class SlackWebClient(Protocol):
    async def chat_postMessage(
        self, *, channel: str, text: str, thread_ts: str | None
    ) -> Mapping[str, object]: ...


class Slack:
    name = "slack"

    def __init__(
        self,
        *,
        bot_token: str,
        app_token: str,
        web_client: SlackWebClient | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._app_token = app_token
        self._web_client = cast(
            SlackWebClient,
            web_client or AsyncWebClient(token=bot_token),
        )
        self._socket_client: SocketModeClient | None = None
        self._receiver: MessageReceiver | None = None
        self._closed = asyncio.Event()

    @classmethod
    def from_env(cls) -> Slack:
        bot_token = os.getenv("SLACK_BOT_TOKEN")
        app_token = os.getenv("SLACK_APP_TOKEN")
        if not bot_token or not app_token:
            raise RuntimeError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN are required")
        return cls(bot_token=bot_token, app_token=app_token)

    def bind(self, receiver: MessageReceiver) -> None:
        self._receiver = receiver

    async def run(self) -> None:
        if self._receiver is None:
            raise RuntimeError("Slack channel is not bound to AgentChat")
        self._socket_client = SocketModeClient(
            app_token=self._app_token,
            web_client=cast(AsyncWebClient, self._web_client),
        )
        listeners = cast(
            list[
                Callable[
                    [AsyncBaseSocketModeClient, SocketModeRequest],
                    Awaitable[None],
                ]
            ],
            self._socket_client.socket_mode_request_listeners,
        )
        listeners.append(self._handle_socket_request)
        connect = cast(Callable[[], Awaitable[None]], self._socket_client.connect)
        await connect()
        await self._closed.wait()

    async def close(self) -> None:
        self._closed.set()
        if self._socket_client is not None:
            close = cast(Callable[[], Awaitable[None]], self._socket_client.close)
            await close()

    async def handle_event(self, payload: Mapping[str, object]) -> None:
        if self._receiver is None:
            raise RuntimeError("Slack channel is not bound to AgentChat")
        event_value = payload.get("event")
        if not isinstance(event_value, Mapping):
            return
        event = cast(Mapping[str, object], event_value)
        message = self._to_message(payload, event)
        if message is not None:
            await self._receiver(self, message)

    async def reply(self, source: Message, content: str) -> Message:
        channel_id = cast(str, source.metadata["channel_id"])
        thread_timestamp = cast(str | None, source.metadata.get("reply_thread_timestamp"))
        response = await self._web_client.chat_postMessage(
            channel=channel_id,
            text=content,
            thread_ts=thread_timestamp,
        )
        response_timestamp = str(response.get("ts", "unknown"))
        return Message(
            id=f"slack:{channel_id}:{response_timestamp}",
            conversation_id=source.conversation_id,
            channel=self.name,
            sender=Sender(id="agentchat", display_name="AgentChat"),
            text=content,
            role="assistant",
            metadata=MappingProxyType(
                {
                    "channel_id": channel_id,
                    "reply_thread_timestamp": thread_timestamp,
                }
            ),
        )

    async def _handle_socket_request(
        self, client: AsyncBaseSocketModeClient, request: SocketModeRequest
    ) -> None:
        if request.type != "events_api":
            return
        await client.send_socket_mode_response(
            SocketModeResponse(envelope_id=request.envelope_id)
        )
        await self.handle_event(request.payload)

    def _to_message(
        self,
        payload: Mapping[str, object],
        event: Mapping[str, object],
    ) -> Message | None:
        event_type = event.get("type")
        is_direct_message = event_type == "message" and event.get("channel_type") == "im"
        if event_type != "app_mention" and not is_direct_message:
            return None
        if event.get("bot_id") is not None or event.get("subtype") is not None:
            return None
        user_id = event.get("user")
        channel_id = event.get("channel")
        timestamp = event.get("ts")
        text = event.get("text")
        if not isinstance(user_id, str):
            return None
        if not isinstance(channel_id, str):
            return None
        if not isinstance(timestamp, str):
            return None
        if not isinstance(text, str):
            return None
        team_id_value = payload.get("team_id", "unknown")
        team_id = team_id_value if isinstance(team_id_value, str) else "unknown"
        thread_timestamp_value = event.get("thread_ts")
        thread_timestamp = (
            thread_timestamp_value if isinstance(thread_timestamp_value, str) else timestamp
        )
        conversation_id = (
            f"slack:{team_id}:{channel_id}"
            if is_direct_message
            else f"slack:{team_id}:{channel_id}:{thread_timestamp}"
        )
        event_id_value = payload.get("event_id")
        event_id = (
            event_id_value
            if isinstance(event_id_value, str)
            else f"{channel_id}:{timestamp}"
        )
        normalized_text = MENTION_PATTERN.sub("", text).strip()
        return Message(
            id=f"slack:{event_id}",
            conversation_id=conversation_id,
            channel=self.name,
            sender=Sender(id=user_id),
            text=normalized_text,
            role="user",
            metadata=MappingProxyType(
                {
                    "channel_id": channel_id,
                    "reply_thread_timestamp": thread_timestamp,
                }
            ),
        )
