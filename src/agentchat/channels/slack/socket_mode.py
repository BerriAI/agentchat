"""Slack Socket Mode ingress, acknowledgement and bounded event lifetime."""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import cast

from slack_sdk.socket_mode.aiohttp import SocketModeClient
from slack_sdk.socket_mode.async_client import AsyncBaseSocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse
from slack_sdk.web.async_client import AsyncWebClient

from agentchat.channels.slack.transport import SlackEventReceiver

logger = logging.getLogger(__name__)
EVENT_DRAIN_TIMEOUT = 20


class SocketMode:
    """An event source that acknowledges Slack envelopes before handler work.

    Pending acknowledgements and handlers share a capacity limit. Overflow is
    left unacknowledged for Slack to retry; accepted work is drained on close.
    Instances have a single run/close lifecycle.
    """

    def __init__(
        self,
        *,
        app_token: str,
        web_client: AsyncWebClient | None = None,
        max_pending_events: int = 64,
    ) -> None:
        if not app_token:
            raise ValueError("app_token is required for Socket Mode")
        if max_pending_events < 1:
            raise ValueError("max_pending_events must be positive")
        self._app_token = app_token
        self._web_client = web_client
        self._max_pending_events = max_pending_events
        self._socket_client: SocketModeClient | None = None
        self._receiver: SlackEventReceiver | None = None
        self._closed = asyncio.Event()
        self._tasks: set[asyncio.Task[None]] = set()
        self._connect_task: asyncio.Task[None] | None = None
        self._close_task: asyncio.Task[None] | None = None
        self._run_started = False

    async def run(self, receiver: SlackEventReceiver) -> None:
        if self._run_started or self._closed.is_set():
            raise RuntimeError("Socket Mode has already been started or closed")
        self._run_started = True
        self._receiver = receiver
        try:
            self._socket_client = SocketModeClient(
                app_token=self._app_token, web_client=self._web_client,
            )
            listeners = cast(
                list[Callable[
                    [AsyncBaseSocketModeClient, SocketModeRequest], Awaitable[None],
                ]],
                self._socket_client.socket_mode_request_listeners,
            )
            listeners.append(self._handle_socket_request)
            connect = cast(Callable[[], Awaitable[None]], self._socket_client.connect)
            self._connect_task = asyncio.create_task(connect())
            try:
                await self._connect_task
            except asyncio.CancelledError:
                if self._closed.is_set():
                    return
                raise
            await self._closed.wait()
        finally:
            await self.close()

    async def close(self) -> None:
        if self._close_task is None:
            self._closed.set()
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self._connect_task is not None and not self._connect_task.done():
            self._connect_task.cancel()
            await asyncio.gather(self._connect_task, return_exceptions=True)
        try:
            if self._socket_client is not None:
                close = cast(Callable[[], Awaitable[None]], self._socket_client.close)
                await close()
        finally:
            if self._tasks:
                _, pending = await asyncio.wait(self._tasks, timeout=EVENT_DRAIN_TIMEOUT)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

    async def is_connected(self) -> bool:
        return (
            not self._closed.is_set()
            and self._socket_client is not None
            and await self._socket_client.is_connected()
        )

    async def _handle_socket_request(
        self, client: AsyncBaseSocketModeClient, request: SocketModeRequest,
    ) -> None:
        if request.type != "events_api" or self._closed.is_set():
            return
        if len(self._tasks) >= self._max_pending_events:
            return
        acknowledged: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        # SDK listener cancellation must not discard a successfully accepted event.
        acknowledged.add_done_callback(
            lambda result: result.exception() if not result.cancelled() else None,
        )
        task = asyncio.create_task(self._deliver_event(client, request, acknowledged))
        self._tasks.add(task)
        task.add_done_callback(self._event_done)
        await asyncio.shield(acknowledged)

    async def _deliver_event(
        self,
        client: AsyncBaseSocketModeClient,
        request: SocketModeRequest,
        acknowledged: asyncio.Future[None],
    ) -> None:
        try:
            await client.send_socket_mode_response(
                SocketModeResponse(envelope_id=request.envelope_id),
            )
        except BaseException as error:
            acknowledged.set_exception(error)
            raise
        acknowledged.set_result(None)
        assert self._receiver is not None
        await self._receiver(request.payload)

    def _event_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.warning("Slack handler failed (%s)", type(error).__name__)
