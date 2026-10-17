from collections.abc import Mapping
from typing import Literal, TypedDict

class SlackSignatureError(PermissionError): ...
class SlackPayloadError(ValueError): ...
class SlackBodyTooLarge(SlackPayloadError): ...

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

class SlackWebhook:
    def __init__(self, secret: str) -> None: ...
    def verify(
        self, raw: bytes, headers: Mapping[str, str], *, now: float | None = ...,
    ) -> None: ...
    def event(
        self, raw: bytes, headers: Mapping[str, str], *, now: float | None = ...,
        max_bytes: int = ...,
    ) -> SlackWebhookEvent: ...
    def interaction(
        self, raw: bytes, headers: Mapping[str, str], *, now: float | None = ...,
        max_bytes: int = ...,
    ) -> dict[str, object]: ...
