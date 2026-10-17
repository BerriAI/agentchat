"""Signed Slack HTTP parsing; hosts own workspace authorization and durable admission.

Read request bodies with a streaming limit before calling these helpers. The SDK
also enforces the supplied byte limit, authenticates the exact raw bytes, and
separates signature, syntax, and size errors without a web framework dependency.
"""
from typing import Literal, TypedDict

from agentchat._native import (
    SlackBodyTooLarge as SlackBodyTooLarge,
)
from agentchat._native import (
    SlackPayloadError as SlackPayloadError,
)
from agentchat._native import (
    SlackSignatureError as SlackSignatureError,
)
from agentchat._native import (
    SlackWebhook as SlackWebhook,
)


class SlackEventEnvelope(TypedDict):
    kind: Literal["event"]
    payload: dict[str, object]


class SlackChallengeEnvelope(TypedDict):
    kind: Literal["challenge"]
    payload: dict[str, object]
    challenge: str


class SlackIgnoredEnvelope(TypedDict):
    kind: Literal["ignored"]
    payload: dict[str, object]


SlackWebhookEvent = SlackEventEnvelope | SlackChallengeEnvelope | SlackIgnoredEnvelope
