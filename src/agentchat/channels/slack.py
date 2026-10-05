from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict, deque
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, cast

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from agentchat.channels.base import MessageReceiver
from agentchat.channels.slack_media import (
    rich_payload,
    text_blocks,
    upload_slack_files,
    validate_destination,
)
from agentchat.channels.slack_mirror import mirror_payload, read_mirror
from agentchat.models import Message, RichReply, Sender, UploadedFile, UploadFile

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SlackUser:
    id: str
    team_id: str
    display_name: str | None
    email: str | None
    is_bot: bool
    deleted: bool


def _profile_text(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    return value.strip() or None


class ThreadSubscriptions(Protocol):
    async def contains(self, conversation_id: str) -> bool: ...

    async def add(self, conversation_id: str) -> None: ...


class MemoryThreadSubscriptions:
    def __init__(self) -> None:
        self._threads: OrderedDict[str, None] = OrderedDict()

    async def contains(self, conversation_id: str) -> bool:
        return conversation_id in self._threads

    async def add(self, conversation_id: str) -> None:
        self._threads[conversation_id] = None
        self._threads.move_to_end(conversation_id)
        if len(self._threads) > 1000:
            self._threads.popitem(last=False)


class SlackWebClient(Protocol):
    async def api_call(self, api_method: str, **kwargs: object) -> Mapping[str, object]: ...

    async def auth_test(self) -> Mapping[str, object]: ...

    async def users_info(self, *, user: str) -> Mapping[str, object]: ...

    async def conversations_replies(self, **kwargs: object) -> Mapping[str, object]: ...

    async def chat_postMessage(self, **kwargs: object) -> Mapping[str, object]: ...

    async def assistant_threads_setStatus(self, **kwargs: object) -> Mapping[str, object]: ...

    async def reactions_add(
        self, *, channel: str, name: str, timestamp: str
    ) -> Mapping[str, object]: ...


class Slack:
    name = "slack"

    def __init__(
        self,
        *,
        bot_token: str,
        app_token: str,
        web_client: SlackWebClient | None = None,
        ack_emoji: str | None = "eyes",
        workspace_id: str | None = None,
        bot_user_id: str | None = None,
        thread_subscriptions: ThreadSubscriptions | None = None,
        max_pending_events: int = 64,
    ) -> None:
        if max_pending_events < 1:
            raise ValueError("max_pending_events must be positive")
        self._bot_token = bot_token
        self._app_token = app_token
        self._web_client = cast(
            SlackWebClient,
            web_client or AsyncWebClient(token=bot_token, retry_handlers=[]),
        )
        self._socket_client: SocketModeClient | None = None
        self._receiver: MessageReceiver | None = None
        self._closed = asyncio.Event()
        self._ack_emoji = ack_emoji
        self._workspace_id = workspace_id
        self._bot_user_id = bot_user_id
        self._threads = thread_subscriptions or MemoryThreadSubscriptions()
        self._max_pending_events = max_pending_events
        self._tasks: set[asyncio.Task[None]] = set()

    @classmethod
    def from_env(cls, *, ack_emoji: str | None = "eyes") -> Slack:
        bot_token = os.getenv("SLACK_BOT_TOKEN")
        app_token = os.getenv("SLACK_APP_TOKEN")
        if not bot_token or not app_token:
            raise RuntimeError("SLACK_BOT_TOKEN and SLACK_APP_TOKEN are required")
        return cls(bot_token=bot_token, app_token=app_token, ack_emoji=ack_emoji)

    def bind(self, receiver: MessageReceiver) -> None:
        self._receiver = receiver

    async def run(self) -> None:
        if self._receiver is None:
            raise RuntimeError("Slack channel is not bound to AgentChat")
        identity = await self._web_client.auth_test()
        workspace_id, bot_user_id = identity.get("team_id"), identity.get("user_id")
        if not isinstance(workspace_id, str) or not isinstance(bot_user_id, str):
            raise RuntimeError("Slack did not return a bot identity")
        if self._workspace_id is not None and self._workspace_id != workspace_id:
            raise RuntimeError("Slack token belongs to a different workspace")
        self._workspace_id, self._bot_user_id = workspace_id, bot_user_id
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
        if self._tasks:
            _, pending = await asyncio.wait(self._tasks, timeout=20)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)

    async def is_connected(self) -> bool:
        return self._socket_client is not None and await self._socket_client.is_connected()

    async def get_user(self, user_id: str) -> SlackUser:
        """Look up a profile on demand; email requires the users:read.email scope."""
        response = await self._web_client.users_info(user=user_id)
        user = response.get("user")
        if response.get("ok") is not True or not isinstance(user, Mapping):
            raise ValueError("Slack did not return a user profile")
        profile = user.get("profile")
        team_id = user.get("team_id")
        deleted, is_bot = user.get("deleted"), user.get("is_bot")
        if (
            user.get("id") != user_id
            or not isinstance(team_id, str)
            or not team_id
            or not isinstance(profile, Mapping)
            or not isinstance(deleted, bool)
            or not isinstance(is_bot, bool)
        ):
            raise ValueError("Slack returned an invalid user profile")
        return SlackUser(
            id=user_id,
            team_id=team_id,
            display_name=(
                _profile_text(profile.get("display_name"))
                or _profile_text(profile.get("real_name"))
                or _profile_text(user.get("name"))
            ),
            email=_profile_text(profile.get("email")),
            is_bot=is_bot or user.get("is_app_user") is True,
            deleted=deleted,
        )

    async def thread_history(self, source: Message, *, limit: int = 50) -> tuple[Message, ...]:
        """Read the root and recent replies before this turn, with Slack sender identities."""
        if not 2 <= limit <= 100:
            raise ValueError("History limit must be between 2 and 100")
        channel = source.metadata["channel_id"]
        thread = source.metadata.get("reply_thread_timestamp")
        current = str(source.metadata["message_timestamp"])
        if not thread or (self._workspace_id and source.metadata["team_id"] != self._workspace_id):
            raise ValueError("Expected a thread in the configured Slack workspace")
        if current == thread:
            return ()
        root: Message | None = None
        recent: deque[Message] = deque(maxlen=limit - 1)
        cursor = ""
        seen: set[str] = set()
        for _ in range(5):
            response = await self._web_client.conversations_replies(
                channel=channel, ts=thread, latest=current, inclusive=False,
                limit=100, cursor=cursor, include_all_metadata=True,
            )
            records = response.get("messages")
            if response.get("ok") is not True or not isinstance(records, list):
                raise ValueError("Slack did not return thread history")
            for record in records:
                if not isinstance(record, Mapping):
                    continue
                ts, text = record.get("ts"), record.get("text")
                sender = record.get("user") or record.get("bot_id")
                if not all(isinstance(value, str) and value for value in (ts, text, sender)):
                    continue
                if float(ts) >= float(current):
                    continue
                mirror = read_mirror(record, bot_user_id=self._bot_user_id)
                item = Message(
                    id=f"slack:{source.metadata['team_id']}:{channel}:{ts}",
                    conversation_id=source.conversation_id, channel=self.name,
                    sender=mirror[0] if mirror else Sender(id=cast(str, sender)),
                    text=mirror[1] if mirror else cast(str, text),
                    role="assistant" if sender == self._bot_user_id and not mirror else "user",
                    thread_id=cast(str, thread),
                    metadata=MappingProxyType({"message_timestamp": ts}),
                )
                if ts == thread:
                    root = item
                else:
                    recent.append(item)
            if not response.get("has_more"):
                return tuple(([root] if root else []) + list(recent))
            metadata = response.get("response_metadata")
            next_cursor = metadata.get("next_cursor") if isinstance(metadata, Mapping) else None
            if not isinstance(next_cursor, str) or not next_cursor or next_cursor in seen:
                raise ValueError("Slack returned invalid history pagination")
            seen.add(next_cursor)
            cursor = next_cursor
        raise ValueError("Slack thread is too long; start a new thread")

    async def subscribe(self, message: Message) -> None:
        """Follow untagged replies after the application accepts a channel conversation."""
        if message.metadata.get("channel_type") != "im":
            await self._threads.add(message.conversation_id)

    async def handle_event(self, payload: Mapping[str, object]) -> None:
        if self._receiver is None:
            raise RuntimeError("Slack channel is not bound to AgentChat")
        event_value = payload.get("event")
        if not isinstance(event_value, Mapping):
            return
        event = cast(Mapping[str, object], event_value)
        message = self._to_message(payload, event)
        if message is not None:
            if message.metadata["requires_subscription"] and not await self._threads.contains(
                message.conversation_id
            ):
                return
            await self._ack(message)
            await self._receiver(self, message)

    async def _ack(self, message: Message) -> None:
        if not self._ack_emoji:
            return
        channel_id = cast(str, message.metadata["channel_id"])
        timestamp = cast(str, message.metadata["message_timestamp"])
        try:
            await self._web_client.reactions_add(
                channel=channel_id, name=self._ack_emoji, timestamp=timestamp
            )
        except Exception:
            pass

    async def set_status(self, source: Message, status: str) -> bool:
        """Set Slack's native working indicator in the existing reply thread.

        Slack prefixes the app name and expires status after two minutes. Empty
        text clears it. Plain DMs return False: setting status there would open a
        new thread, changing the destination where users expect the answer.
        """
        if source.channel != self.name or (
            self._workspace_id and source.metadata.get("team_id") != self._workspace_id
        ):
            raise ValueError("Expected a source in the configured Slack workspace")
        channel_id = source.metadata.get("channel_id")
        thread = source.metadata.get("reply_thread_timestamp")
        if not isinstance(channel_id, str) or not channel_id:
            raise ValueError("Expected a Slack channel destination")
        if thread is None:
            return False
        if not isinstance(thread, str) or not thread:
            raise ValueError("Expected a Slack thread destination")
        response = await self._web_client.assistant_threads_setStatus(
            channel_id=channel_id, thread_ts=thread, status=status,
        )
        if response.get("ok") is not True:
            raise RuntimeError("Slack working status could not be confirmed")
        return True

    def _delivery_destination(self, source: Message) -> tuple[str, str | None]:
        if source.channel != self.name or (
            self._workspace_id and source.metadata.get("team_id") != self._workspace_id
        ):
            raise ValueError("Expected a source in the configured Slack workspace")
        channel_id = source.metadata.get("channel_id")
        thread = source.metadata.get("reply_thread_timestamp")
        if not isinstance(channel_id, str) or not channel_id:
            raise ValueError("Expected a Slack channel destination")
        if thread is not None and (not isinstance(thread, str) or not thread):
            raise ValueError("Expected a Slack thread destination")
        validate_destination(channel_id, thread)
        return channel_id, thread

    async def upload_files(
        self, source: Message, files: Sequence[UploadFile],
    ) -> tuple[UploadedFile, ...]:
        channel_id, thread = self._delivery_destination(source)

        async def request(api: str, payload: dict[str, object]) -> Mapping[str, object]:
            body = {"data": payload} if api == "files.getUploadURLExternal" else {"json": payload}
            return await self._web_client.api_call(api, http_verb="POST", **body)

        return await upload_slack_files(files, request=request, channel_id=channel_id,
                                        thread_ts=thread)

    async def reply_rich(self, source: Message, content: RichReply) -> Message:
        channel_id, thread = self._delivery_destination(source)
        response = await self._web_client.chat_postMessage(
            channel=channel_id, thread_ts=thread, **rich_payload(content),
        )
        timestamp = response.get("ts")
        if response.get("ok") is not True or not isinstance(timestamp, str) or not timestamp:
            raise RuntimeError("Slack rich reply delivery could not be confirmed")
        return Message(
            id=f"slack:{channel_id}:{timestamp}", conversation_id=source.conversation_id,
            channel=self.name, sender=Sender(id="agentchat", display_name="AgentChat"),
            text=content.text, role="assistant", thread_id=source.thread_id,
            metadata=MappingProxyType({"channel_id": channel_id,
                "reply_thread_timestamp": thread, "message_timestamp": timestamp}),
        )

    async def reply(self, source: Message, content: str) -> Message:
        channel_id = cast(str, source.metadata["channel_id"])
        thread_timestamp = cast(str | None, source.metadata.get("reply_thread_timestamp"))
        response = await self._web_client.chat_postMessage(
            channel=channel_id,
            text=content,
            blocks=text_blocks(content),
            thread_ts=thread_timestamp,
            unfurl_links=False,
            unfurl_media=False,
        )
        response_timestamp = str(response.get("ts", "unknown"))
        return Message(
            id=f"slack:{channel_id}:{response_timestamp}",
            conversation_id=source.conversation_id,
            channel=self.name,
            sender=Sender(id="agentchat", display_name="AgentChat"),
            text=content,
            role="assistant",
            thread_id=source.thread_id,
            metadata=MappingProxyType(
                {
                    "channel_id": channel_id,
                    "reply_thread_timestamp": thread_timestamp,
                    "message_timestamp": response_timestamp,
                }
            ),
        )

    async def mirror(self, source: Message, message: Message, *, origin: str = "web") -> Message:
        if source.channel != self.name or (
            self._workspace_id and source.metadata.get("team_id") != self._workspace_id
        ):
            raise ValueError("Expected a source in the configured Slack workspace")
        payload = mirror_payload(message, origin=origin)
        channel_id = source.metadata["channel_id"]
        thread = source.metadata.get("reply_thread_timestamp")
        response = await self._web_client.chat_postMessage(
            channel=channel_id, thread_ts=thread, **payload,
        )
        timestamp = response.get("ts")
        if response.get("ok") is not True or not isinstance(timestamp, str) or not timestamp:
            raise RuntimeError("Slack mirror delivery could not be confirmed")
        return Message(
            id=f"slack:{channel_id}:{timestamp}", conversation_id=source.conversation_id,
            channel=self.name, sender=message.sender, text=message.text, role="user",
            thread_id=source.thread_id,
            metadata=MappingProxyType({
                "channel_id": channel_id, "reply_thread_timestamp": thread,
                "message_timestamp": timestamp, "mirrored_message_id": message.id,
                "origin": origin,
            }),
        )

    async def _handle_socket_request(
        self, client: AsyncBaseSocketModeClient, request: SocketModeRequest
    ) -> None:
        if request.type != "events_api":
            return
        if self._closed.is_set() or len(self._tasks) >= self._max_pending_events:
            return
        await client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        task = asyncio.create_task(self.handle_event(request.payload))
        self._tasks.add(task)
        task.add_done_callback(self._event_done)

    def _event_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("Slack handler failed (%s)", type(error).__name__)

    def _to_message(
        self,
        payload: Mapping[str, object],
        event: Mapping[str, object],
    ) -> Message | None:
        event_type = event.get("type")
        is_direct_message = event_type == "message" and event.get("channel_type") == "im"
        if event.get("bot_id") is not None or event.get("subtype") is not None:
            return None
        user_id = event.get("user")
        channel_id = event.get("channel")
        timestamp = event.get("ts")
        text = event.get("text")
        if not isinstance(user_id, str) or not user_id or user_id == self._bot_user_id:
            return None
        if not isinstance(channel_id, str) or not channel_id:
            return None
        if not isinstance(timestamp, str) or not timestamp:
            return None
        if not isinstance(text, str) or not text.strip():
            return None
        team_id = payload.get("team_id")
        if not isinstance(team_id, str) or not team_id:
            return None
        if self._workspace_id is not None and team_id != self._workspace_id:
            return None
        thread_timestamp_value = event.get("thread_ts")
        if thread_timestamp_value is not None and (
            not isinstance(thread_timestamp_value, str) or not thread_timestamp_value
        ):
            return None
        thread_timestamp = (
            thread_timestamp_value if isinstance(thread_timestamp_value, str) else timestamp
        )
        bot_mention = f"<@{self._bot_user_id}>" if self._bot_user_id else None
        is_channel_message = event_type == "message" and event.get("channel_type") in (
            "channel",
            "group",
        )
        is_mention = event_type == "app_mention" or (
            is_channel_message and bot_mention is not None and bot_mention in text
        )
        is_thread_reply = is_channel_message and thread_timestamp_value is not None
        if not (is_direct_message or is_mention or is_thread_reply):
            return None
        conversation_id = (
            f"slack:{team_id}:{channel_id}"
            if is_direct_message
            else f"slack:{team_id}:{channel_id}:{thread_timestamp}"
        )
        event_id_value = payload.get("event_id")
        event_id = (
            event_id_value if isinstance(event_id_value, str) else f"{channel_id}:{timestamp}"
        )
        normalized_text = text.replace(bot_mention, "").strip() if bot_mention else text.strip()
        if not normalized_text:
            return None
        return Message(
            id=f"slack:{team_id}:{channel_id}:{timestamp}",
            conversation_id=conversation_id,
            channel=self.name,
            sender=Sender(id=user_id),
            text=normalized_text,
            role="user",
            thread_id=thread_timestamp_value if is_direct_message else thread_timestamp,
            addressed=is_direct_message or is_mention,
            metadata=MappingProxyType(
                {
                    "channel_id": channel_id,
                    "team_id": team_id,
                    "event_id": event_id,
                    "channel_type": "im"
                    if is_direct_message
                    else event.get("channel_type", "channel"),
                    "reply_thread_timestamp": thread_timestamp_value
                    if is_direct_message
                    else thread_timestamp,
                    "message_timestamp": timestamp,
                    "requires_subscription": not is_direct_message and not is_mention,
                }
            ),
        )
