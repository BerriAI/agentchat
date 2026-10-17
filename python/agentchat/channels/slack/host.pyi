from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Literal, NotRequired, TypedDict

class SlackRouting(TypedDict):
    text: str
    mentioned: bool
    excluded: bool

class ThreadOrigin(TypedDict):
    team_id: str
    channel_id: str
    thread_ts: str
    message_ts: str

class CurrentReference(ThreadOrigin):
    user_id: str
    data: dict[str, object]

class SlackHostEvent(ThreadOrigin, SlackRouting):
    user_id: str
    direct: bool
    files: list[dict[str, object]]
    reference: CurrentReference

class ContextMessage(TypedDict):
    ts: str
    author: str
    content: str
    truncated: bool

class ThreadContext(ThreadOrigin):
    status: Literal["complete", "partial", "unavailable"]
    messages: list[ContextMessage]
    error: NotRequired[str]
    warning: NotRequired[str]

class SlackHost:
    def __init__(
        self, *, bot_user_id: str, workspace_id: str | None = ...,
        exclude_prefixes: Sequence[str] = ...,
    ) -> None: ...
    def routing_text(self, text: str) -> SlackRouting: ...
    def normalize_event(self, payload: Mapping[str, object]) -> SlackHostEvent | None: ...
    async def thread_context(
        self, origin: Mapping[str, object],
        request_page: Callable[[dict[str, object]], Awaitable[Mapping[str, object]]] | None = ...,
        *,
        max_pages: int = ..., max_chars: int = ..., max_message_chars: int = ...,
        timeout: float = ...,  # noqa: ASYNC109 - bounded native callback protocol
    ) -> ThreadContext: ...
