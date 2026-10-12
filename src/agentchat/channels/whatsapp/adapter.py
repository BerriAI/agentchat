from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
from types import MappingProxyType

import aiohttp
from aiohttp import web

from agentchat.channels._delivery import DeliveryTasks
from agentchat.channels.base import MessageReceiver
from agentchat.channels.whatsapp.events import normalize
from agentchat.channels.whatsapp.rendering import text_payload
from agentchat.models import Message, Sender

GRAPH_ROOT = "https://graph.facebook.com"


class WhatsApp:
    """WhatsApp Business Cloud API: signed webhooks and text replies."""

    name = "whatsapp"

    def __init__(
        self,
        *,
        phone_number_id: str,
        access_token: str,
        app_secret: str,
        verify_token: str,
        api_version: str = "v23.0",
        host: str = "127.0.0.1",
        port: int = 3979,
        max_pending_events: int = 64,
        shutdown_timeout: float = 20,
    ) -> None:
        if not all(
            value.strip() for value in (phone_number_id, access_token, app_secret, verify_token)
        ):
            raise ValueError(
                "WhatsApp phone_number_id, access_token, app_secret, verify_token required"
            )
        if not re.fullmatch(r"[0-9]+", phone_number_id):
            raise ValueError("WhatsApp phone_number_id must be the numeric API ID")
        if not re.fullmatch(r"v[1-9][0-9]*\.[0-9]+", api_version):
            raise ValueError("WhatsApp api_version must have the form v23.0")
        self._phone_number_id, self._access_token = phone_number_id, access_token
        self._app_secret, self._verify_token = app_secret, verify_token
        self._api_version, self._host, self._port = api_version, host, port
        self._delivery = DeliveryTasks(max_pending_events, shutdown_timeout)
        self._session: aiohttp.ClientSession | None = None
        self._runner: web.AppRunner | None = None
        self._stopped = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    def from_env(cls) -> WhatsApp:
        keys = (
            "WHATSAPP_PHONE_NUMBER_ID",
            "WHATSAPP_ACCESS_TOKEN",
            "WHATSAPP_APP_SECRET",
            "WHATSAPP_VERIFY_TOKEN",
        )
        values = [os.getenv(key) for key in keys]
        if not all(values):
            raise RuntimeError(f"{', '.join(keys)} are required")
        return cls(
            phone_number_id=values[0],
            access_token=values[1],
            app_secret=values[2],
            verify_token=values[3],
            api_version=os.getenv("WHATSAPP_API_VERSION", "v23.0"),
            host=os.getenv("WHATSAPP_HOST", "127.0.0.1"),
            port=int(os.getenv("WHATSAPP_PORT", "3979")),
        )

    def bind(self, receiver: MessageReceiver) -> None:
        self._delivery.receiver = receiver

    def create_app(self) -> web.Application:
        app = web.Application()
        app.router.add_get("/webhooks/whatsapp", self._verify)
        app.router.add_post("/webhooks/whatsapp", self._receive)
        return app

    async def run(self) -> None:
        self._delivery.check_bound()
        self._runner = web.AppRunner(self.create_app(), access_log=None)
        try:
            await self._runner.setup()
            await web.TCPSite(self._runner, self._host, self._port).start()
            await self._stopped.wait()
        finally:
            await self.close()

    async def close(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._delivery.closed = True
            self._stopped.set()
            try:
                if self._runner:
                    await self._runner.cleanup()
            finally:
                try:
                    await self._delivery.close()
                finally:
                    if self._session:
                        await self._session.close()

    async def _verify(self, request: web.Request) -> web.Response:
        query = request.query
        if (
            query.get("hub.mode") != "subscribe"
            or not query.get("hub.challenge")
            or not hmac.compare_digest(
                query.get("hub.verify_token", "").encode(),
                self._verify_token.encode(),
            )
        ):
            raise web.HTTPForbidden(text="Webhook verification failed")
        return web.Response(text=query["hub.challenge"])

    async def _receive(self, request: web.Request) -> web.Response:
        raw = await request.read()
        signature = "sha256=" + hmac.new(self._app_secret.encode(), raw, hashlib.sha256).hexdigest()
        supplied = request.headers.get("X-Hub-Signature-256", "")
        if not hmac.compare_digest(supplied.encode(), signature.encode()):
            raise web.HTTPUnauthorized(text="Invalid webhook signature")
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(text="Invalid webhook JSON") from None
        messages = normalize(payload, phone_number_id=self._phone_number_id)
        # Admit a batch atomically so a 503 never follows a partially accepted batch.
        if (
            self._delivery.closed
            or len(self._delivery.tasks) + len(messages) > self._delivery.limit
        ):
            raise web.HTTPServiceUnavailable(text="WhatsApp receiver is busy")
        for message in messages:
            self._delivery.submit(self, message)
        return web.Response(text="OK")

    async def reply(self, source: Message, content: str) -> Message:
        data = source.metadata
        recipient, message_id = data.get("wa_id"), data.get("message_id")
        if (
            source.channel != self.name
            or data.get("phone_number_id") != self._phone_number_id
            or not isinstance(recipient, str)
            or not re.fullmatch(r"[0-9]+", recipient)
            or not isinstance(message_id, str)
            or not message_id
        ):
            raise ValueError("Source does not belong to this WhatsApp adapter")
        payload = text_payload(content, recipient=recipient, message_id=message_id)
        self._delivery.check_reply()
        if self._session is None:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))
        try:
            async with self._session.post(
                f"{GRAPH_ROOT}/{self._api_version}/{self._phone_number_id}/messages",
                headers={"Authorization": f"Bearer {self._access_token}"},
                json=payload,
                allow_redirects=False,
            ) as response:
                if not 200 <= response.status < 300:
                    raise RuntimeError(f"WhatsApp send failed (HTTP {response.status})")
                result = await response.json()
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise RuntimeError("WhatsApp request failed; delivery may be uncertain") from None
        receipts = result.get("messages") if isinstance(result, dict) else None
        receipt_id = (
            receipts[0].get("id")
            if (isinstance(receipts, list) and len(receipts) == 1 and isinstance(receipts[0], dict))
            else None
        )
        if not isinstance(receipt_id, str) or not receipt_id:
            raise RuntimeError("WhatsApp did not return a message receipt")
        return Message(
            id=f"whatsapp:{self._phone_number_id}:{receipt_id}",
            conversation_id=source.conversation_id,
            channel=self.name,
            sender=Sender(id=self._phone_number_id),
            text=content,
            role="assistant",
            metadata=MappingProxyType({**data, "message_id": receipt_id}),
        )
