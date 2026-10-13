# Public compatibility signatures; update with the corresponding Rust binding API.
from collections.abc import Mapping

from agentchat.models import Message, Sender

EVENT_TYPE: str

def mirror_payload(message: Message, *, origin: str = ...) -> dict[str, object]: ...
def read_mirror(
    record: Mapping[str, object], *, bot_user_id: str | None
) -> tuple[Sender, str] | None: ...
