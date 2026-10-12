from __future__ import annotations

import asyncio
import os
from types import MappingProxyType
from urllib.parse import quote, urlsplit

from aiohttp import web
from microsoft_agents.activity import Activity
from microsoft_agents.authentication.msal import MsalConnectionManager
from microsoft_agents.hosting.aiohttp import CloudAdapter, jwt_authorization_middleware
from microsoft_agents.hosting.core import AgentAuthConfiguration, OutboundHostValidator, TurnContext
from pydantic import ValidationError

from agentchat._delivery import DeliveryTasks
from agentchat.channels.base import MessageReceiver
from agentchat.channels.teams.events import normalize
from agentchat.channels.teams.rendering import text_payload
from agentchat.models import Message, Sender


class Teams:
    """Single-tenant Teams bot using the Microsoft 365 Agents SDK (public cloud)."""

    name = "teams"

    def __init__(
        self,
        *,
        app_id: str,
        app_password: str,
        tenant_id: str,
        host: str = "127.0.0.1",
        port: int = 3978,
        adapter: CloudAdapter | None = None,
        max_pending_events: int = 64,
        shutdown_timeout: float = 20,
    ) -> None:
        if not all(value.strip() for value in (app_id, app_password, tenant_id)):
            raise ValueError("Teams app_id, app_password, and tenant_id are required")
        self._app_id, self._tenant_id = app_id, tenant_id
        self._host, self._port = host, port
        self._delivery = DeliveryTasks(max_pending_events, shutdown_timeout)
        self._auth = AgentAuthConfiguration(
            client_id=app_id,
            client_secret=app_password,
            tenant_id=tenant_id,
            anonymous_allowed=False,
            validate_issuer=True,
        )
        self._hosts = OutboundHostValidator(enabled=True)
        self._adapter = adapter or CloudAdapter(
            connection_manager=MsalConnectionManager(
                connections_configurations={"SERVICE_CONNECTION": self._auth},
            ),
            host_validator=self._hosts,
        )
        self._adapter.on_turn_error = self._turn_error
        self._runner: web.AppRunner | None = None
        self._stopped = asyncio.Event()
        self._close_lock = asyncio.Lock()
        self._closed = False

    @classmethod
    def from_env(cls) -> Teams:
        keys = ("TEAMS_APP_ID", "TEAMS_APP_PASSWORD", "TEAMS_TENANT_ID")
        values = [os.getenv(key) for key in keys]
        if not all(values):
            raise RuntimeError(f"{', '.join(keys)} are required")
        return cls(
            app_id=values[0],
            app_password=values[1],
            tenant_id=values[2],
            host=os.getenv("TEAMS_HOST", "127.0.0.1"),
            port=int(os.getenv("TEAMS_PORT", "3978")),
        )

    def bind(self, receiver: MessageReceiver) -> None:
        self._delivery.receiver = receiver

    def create_app(self) -> web.Application:
        """Host this app yourself, or use run(). Authentication is always installed."""
        app = web.Application(middlewares=[jwt_authorization_middleware])
        # These keys are part of Microsoft's aiohttp middleware contract.
        app["agent_configuration"] = self._auth
        app.router.add_post("/api/messages", self._handle_request)
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
                await self._delivery.close()

    async def _handle_request(self, request: web.Request) -> web.Response:
        if self._delivery.closed:
            raise web.HTTPServiceUnavailable()
        try:
            event = await request.json()
            if not isinstance(event, dict) or not isinstance(event.get("serviceUrl"), str):
                raise ValueError("Missing Teams service URL")
            # Validate before CloudAdapter allocates any token-bearing connector clients.
            self._validate_service_url(event["serviceUrl"])
            return await self._adapter.process(request, self)
        except (ValidationError, ValueError, TypeError):
            raise web.HTTPBadRequest(text="Invalid Teams activity") from None

    @staticmethod
    async def _turn_error(context: TurnContext, error: Exception) -> None:
        # Microsoft's default sends exception details to the conversation.
        raise error

    async def on_turn(self, context: TurnContext) -> None:
        """Called by the authenticated Microsoft adapter, never directly by ingress."""
        event = context.activity.model_dump(by_alias=True, exclude_none=True)
        message = normalize(event, app_id=self._app_id, tenant_id=self._tenant_id)
        if message is None:
            return
        self._validate_service_url(event["serviceUrl"])
        if not self._delivery.submit(self, message):
            raise web.HTTPServiceUnavailable(text="Teams receiver is busy")

    def _validate_service_url(self, url: str) -> None:
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
            or parts.port not in (None, 443)
            or not self._hosts.is_allowed(url)
        ):
            raise ValueError("Teams service URL is not an allowed HTTPS Microsoft endpoint")

    async def reply(self, source: Message, content: str) -> Message:
        content = text_payload(content)
        data = source.metadata
        route = data.get("continuation_activity")
        if (
            source.channel != self.name
            or data.get("app_id") != self._app_id
            or data.get("tenant_id") != self._tenant_id
            or not isinstance(route, dict)
        ):
            raise ValueError("Source does not belong to this Teams adapter")
        activity = Activity.model_validate(route)
        if (
            not activity.conversation
            or not activity.conversation.id
            or not activity.recipient
            or activity.recipient.id not in (self._app_id, f"28:{self._app_id}")
            or route.get("channelData", {}).get("tenant", {}).get("id") != self._tenant_id
        ):
            raise ValueError("Invalid Teams conversation reference")
        self._validate_service_url(activity.service_url)
        self._delivery.check_reply()
        receipt_id = None

        async def send(context: TurnContext) -> None:
            nonlocal receipt_id
            receipt = await context.send_activity(
                Activity(
                    type="message",
                    text=content,
                    text_format="plain",
                )
            )
            receipt_id = receipt.id if receipt else None

        await self._adapter.continue_conversation(self._app_id, activity, send)
        if not isinstance(receipt_id, str) or not receipt_id:
            raise RuntimeError("Teams did not return a message receipt")
        return Message(
            id=f"teams:{self._app_id}:{self._tenant_id}:"
            f"{quote(activity.conversation.id, safe='')}:{receipt_id}",
            conversation_id=source.conversation_id,
            channel=self.name,
            sender=Sender(id=activity.recipient.id, display_name=activity.recipient.name),
            text=content,
            role="assistant",
            thread_id=source.thread_id,
            metadata=MappingProxyType({**data, "activity_id": receipt_id}),
        )
