"""Event-source contract for composing Slack with host-owned transports."""
from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Protocol

SlackEventReceiver = Callable[[Mapping[str, object]], Awaitable[None]]


class SlackEventSource(Protocol):
    """Deliver authenticated Events API payloads to the shared Slack adapter.

    ``run`` stays active until shutdown and propagates startup/transport errors.
    The source owns acknowledgement, backpressure and accepted background work;
    ``close`` must stop intake and drain or cancel that work within a bound. It
    must be safe to close before startup or more than once. Host HTTP sources
    verify Slack signatures before delivery; the receiver does not authenticate
    raw requests. A source instance is owned by one Slack adapter.
    """

    async def run(self, receiver: SlackEventReceiver) -> None: ...

    async def close(self) -> None: ...

    async def is_connected(self) -> bool:
        """Whether the source is currently ready to deliver authenticated events."""
        ...
