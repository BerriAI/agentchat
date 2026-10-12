from __future__ import annotations

import asyncio
import hashlib
import logging
import os
from collections.abc import Collection
from types import MappingProxyType
from urllib.parse import quote

import socketio

from agentchat.channels._delivery import DeliveryTasks
from agentchat.channels.base import MessageReceiver
from agentchat.channels.imessage.events import normalize
from agentchat.channels.imessage.rendering import text_payload
from agentchat.channels.imessage.transport import BlueBubblesClient
from agentchat.models import Message, Sender

logger = logging.getLogger(__name__)


class IMessage:
    """iMessage through a BlueBubbles server, using Socket.IO and REST."""

    name = "imessage"

    def __init__(
        self,
        *,
        server_url: str,
        password: str,
        instance_id: str | None = None,
        allowed_chat_guids: Collection[str] | None = None,
        client: BlueBubblesClient | None = None,
        socket_client: socketio.AsyncClient | None = None,
        max_pending_events: int = 64,
        shutdown_timeout: float = 20,
    ) -> None:
        if not password.strip():
            raise ValueError("BlueBubbles password is required")
        self._client = client or BlueBubblesClient(server_url, password)
        self._instance_id = (
            instance_id
            or hashlib.sha256(
                self._client.server_url.encode(),
            ).hexdigest()[:24]
        )
        self._allowed_chats = None if allowed_chat_guids is None else frozenset(allowed_chat_guids)
        self._delivery = DeliveryTasks(max_pending_events, shutdown_timeout)
        # Engine.IO can log full password-bearing URLs, even with logger=False.
        socket_logger = logging.Logger(f"{__name__}.socket", level=logging.CRITICAL + 1)
        socket_logger.propagate = False
        self._socket = socket_client or socketio.AsyncClient(
            reconnection=True,
            logger=socket_logger,
            engineio_logger=socket_logger,
            handle_sigint=False,
        )
        self._socket.on("new-message", self._on_message)
        self._close_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    def from_env(cls) -> IMessage:
        url, password = os.getenv("BLUEBUBBLES_URL"), os.getenv("BLUEBUBBLES_PASSWORD")
        if not url or not password:
            raise RuntimeError("BLUEBUBBLES_URL and BLUEBUBBLES_PASSWORD are required")
        chats = os.getenv("IMESSAGE_ALLOWED_CHATS")
        return cls(
            server_url=url,
            password=password,
            instance_id=os.getenv("IMESSAGE_INSTANCE_ID"),
            allowed_chat_guids=[chat.strip() for chat in chats.split(",")] if chats else None,
        )

    def bind(self, receiver: MessageReceiver) -> None:
        self._delivery.receiver = receiver

    async def run(self) -> None:
        self._delivery.check_bound()
        try:
            await self._client.check_connection()
            try:
                await self._socket.connect(self._client.socket_url)
                await self._socket.wait()
            except Exception:
                raise RuntimeError("BlueBubbles socket connection failed") from None
            if not self._closed:
                raise RuntimeError("BlueBubbles socket disconnected; check the server and password")
        finally:
            await self.close()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._delivery.closed = True
            try:
                await self._socket.shutdown()
            finally:
                try:
                    await self._delivery.close()
                finally:
                    await self._client.close()

    async def _on_message(self, event: object) -> None:
        message = normalize(event, instance_id=self._instance_id)
        if message is None or self._delivery.closed:
            return
        if (
            self._allowed_chats is not None
            and message.metadata["chat_guid"] not in self._allowed_chats
        ):
            return
        if not self._delivery.submit(self, message):
            logger.warning("iMessage event dropped: max_pending_events reached")

    async def reply(self, source: Message, content: str) -> Message:
        content = text_payload(content)
        data = source.metadata
        chat_guid = data.get("chat_guid")
        if (
            source.channel != self.name
            or data.get("instance_id") != self._instance_id
            or not isinstance(chat_guid, str)
            or not chat_guid.startswith("iMessage;")
        ):
            raise ValueError("Source does not belong to this iMessage adapter")
        if self._allowed_chats is not None and chat_guid not in self._allowed_chats:
            raise ValueError("iMessage conversation is not allowed")
        self._delivery.check_reply()
        receipt_id = await self._client.send_text(chat_guid, content)
        if not isinstance(receipt_id, str) or not receipt_id:
            raise RuntimeError("BlueBubbles did not return a message receipt")
        return Message(
            id=f"imessage:{quote(self._instance_id, safe='')}:{receipt_id}",
            conversation_id=source.conversation_id,
            channel=self.name,
            sender=Sender(id=f"imessage:{self._instance_id}:me"),
            text=content,
            role="assistant",
            metadata=MappingProxyType({**data, "message_guid": receipt_id}),
        )
