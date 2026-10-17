from collections.abc import Awaitable, Callable, Mapping
from typing import Literal, TypedDict

from agentchat.models import RichReply

class SlackDeliveryResult(TypedDict):
    status: Literal["confirmed", "rejected", "uncertain"]
    receipt: dict[str, object] | None
    reason: str | None

class SlackDelivery:
    def __init__(
        self,
        request: Callable[[str, str, dict[str, object]], Awaitable[Mapping[str, object]]],
        *, transport_errors: tuple[type[Exception], ...] = ...,
    ) -> None: ...
    async def send(
        self, reply: RichReply, *, channel_id: str, thread_ts: str | None = ...,
        message_ts: str | None = ..., metadata: Mapping[str, object] | None = ...,
        fallback_blocks: bool = ...,
    ) -> SlackDeliveryResult: ...
    async def send_request(self, request: Mapping[str, object]) -> SlackDeliveryResult: ...
    async def set_status(
        self, channel_id: str, thread_ts: str, status: str,
    ) -> SlackDeliveryResult: ...
    async def reconcile(
        self, channel_id: str, thread_ts: str, *, event_type: str, event_id: str,
        max_pages: int = ...,
        timeout: float = ...,  # noqa: ASYNC109 - bounded native callback protocol
    ) -> SlackDeliveryResult: ...
