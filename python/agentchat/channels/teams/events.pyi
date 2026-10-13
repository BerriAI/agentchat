from collections.abc import Mapping
from typing import Any

from agentchat.models import Message

def normalize(event: Mapping[str, Any], *, app_id: str, tenant_id: str) -> Message | None: ...
