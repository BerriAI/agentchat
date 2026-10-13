"""Compatible Python declarations for the native Slack implementation."""
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from slack_sdk.web.async_client import AsyncWebClient as AsyncWebClient

from agentchat._native import MemoryThreadSubscriptions as MemoryThreadSubscriptions
from agentchat._native import Slack as Slack


@dataclass(frozen=True, slots=True)
class SlackUser:
    id: str
    team_id: str
    display_name: str | None
    email: str | None
    is_bot: bool
    deleted: bool


class ThreadSubscriptions(Protocol):
    async def contains(self, conversation_id: str) -> bool: ...

    async def add(self, conversation_id: str) -> None: ...


class SlackWebClient(Protocol):
    async def api_call(self, api_method: str, **kwargs: object) -> Mapping[str, object]: ...

    async def auth_test(self) -> Mapping[str, object]: ...

    async def users_info(self, *, user: str) -> Mapping[str, object]: ...

    async def conversations_replies(self, **kwargs: object) -> Mapping[str, object]: ...

    async def chat_postMessage(self, **kwargs: object) -> Mapping[str, object]: ...

    async def assistant_threads_setStatus(self, **kwargs: object) -> Mapping[str, object]: ...

    async def reactions_add(
        self, *, channel: str, name: str, timestamp: str
    ) -> Mapping[str, object]: ...
