# Public compatibility signatures; update with the corresponding Rust binding API.
from collections.abc import Mapping
from typing import Literal, TypedDict

from agentchat.models import RichReply

class SlackMessageRequest(TypedDict):
    method: Literal["chat.postMessage", "chat.update"]
    payload: dict[str, object]

def text_blocks(text: str) -> list[dict[str, object]]: ...
def rich_payload(reply: RichReply) -> dict[str, object]: ...
def message_request(
    reply: RichReply, *, channel_id: str, thread_ts: str | None = ...,
    message_ts: str | None = ..., metadata: Mapping[str, object] | None = ...,
    fallback_blocks: bool = ...,
) -> SlackMessageRequest: ...
