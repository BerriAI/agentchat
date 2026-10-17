"""One-attempt Slack sends and bounded read reconciliation, implemented in Rust.

The async request callback receives ``(http_method, api_method, payload)`` and
returns the parsed provider response. Supply transport exception classes through
``transport_errors``; other exceptions and cancellation propagate. A send is
never retried. Hosts retain authorization, durable claims, and receipt storage.

``send_request`` accepts the plan from ``rendering.message_request`` so the host
can prepare a validated request before acquiring its durable sending claim.
``reconcile`` matches both metadata event type and id. Absence or failed reads
remain uncertain and never authorize another write.
"""
from typing import Literal, TypedDict

from agentchat._native import SlackDelivery as SlackDelivery


class SlackDeliveryResult(TypedDict):
    status: Literal["confirmed", "rejected", "uncertain"]
    receipt: dict[str, object] | None
    reason: str | None
