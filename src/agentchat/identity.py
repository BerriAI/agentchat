"""Opt-in identity provisioning for applications that own their account and SSO store."""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Collection
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Generic, Protocol, TypeVar
from weakref import WeakValueDictionary

from agentchat.channels.slack import SlackUser
from agentchat.models import Message

logger = logging.getLogger(__name__)
Account = TypeVar("Account")


class SlackUserLookup(Protocol):
    async def get_user(self, user_id: str) -> SlackUser: ...


@dataclass(frozen=True, slots=True)
class SlackIdentity:
    """A workspace member's profile, not proof of a Google or web login.

    Persist ``key`` as the external identity. Email is a matching attribute and
    can change; it must never replace the provider's stable identifiers.
    """

    workspace_id: str
    user_id: str
    email: str
    display_name: str | None
    checked_at: datetime

    @property
    def key(self) -> tuple[str, str, str]:
        return ("slack", self.workspace_id, self.user_id)


@dataclass(frozen=True, slots=True)
class _CachedIdentity:
    expires_at: float
    identity: SlackIdentity | None


class SlackIdentityProvisioner(Generic[Account]):
    """Resolve an accepted sender and call an application's idempotent account upsert.

    Invoke ``provision`` after authorizing a received message. The callback owns
    durable storage and matching to verified SSO identities. Successful profiles
    are cached; callbacks run on every invocation so a later SSO login can be
    picked up without refreshing Slack. Profile failures return None, while
    callback failures propagate so applications can retry or choose a fallback.
    """

    def __init__(
        self,
        slack: SlackUserLookup,
        *,
        workspace_id: str,
        allowed_email_domains: Collection[str],
        on_identity: Callable[[SlackIdentity], Awaitable[Account]],
        cache_ttl_seconds: float = 3600,
        retry_seconds: float = 60,
        lookup_timeout_seconds: float = 5,
        max_cache_entries: int = 1000,
    ) -> None:
        if not workspace_id or workspace_id != workspace_id.strip():
            raise ValueError("workspace_id must be a non-empty workspace ID")
        if isinstance(allowed_email_domains, (str, bytes)) or not allowed_email_domains:
            raise ValueError("allowed_email_domains must be a non-empty collection of domains")
        domains = frozenset(domain.strip().lower() for domain in allowed_email_domains)
        if any(not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", domain) for domain in domains):
            raise ValueError("Use exact email domains, without wildcards or @")
        for value in (cache_ttl_seconds, retry_seconds, lookup_timeout_seconds):
            if not math.isfinite(value) or value <= 0:
                raise ValueError("Cache, retry and lookup timeouts must be positive and finite")
        if type(max_cache_entries) is not int or max_cache_entries < 1:
            raise ValueError("max_cache_entries must be a positive integer")
        self._slack = slack
        self._workspace_id = workspace_id
        self._domains = domains
        self._on_identity = on_identity
        self._cache_ttl = cache_ttl_seconds
        self._retry_seconds = retry_seconds
        self._lookup_timeout = lookup_timeout_seconds
        self._max_cache_entries = max_cache_entries
        self._cache: OrderedDict[str, _CachedIdentity] = OrderedDict()
        self._locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()

    async def provision(self, message: Message) -> Account | None:
        """Upsert an eligible sender; return the application's account, or None.

        Does not alter the message, sender ID, history, authentication or roles.
        A missing scope, timeout or ineligible profile never invokes the callback.
        """
        identity = await self.resolve(message)
        return await self._on_identity(identity) if identity is not None else None

    async def resolve(self, message: Message) -> SlackIdentity | None:
        """Get an eligible cached Slack profile without provisioning an account."""
        if (
            message.channel != "slack"
            or message.role != "user"
            or message.metadata.get("team_id") != self._workspace_id
            or not message.sender.id
        ):
            return None
        user_id = message.sender.id
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            cached = self._cache.get(user_id)
            if cached is not None and cached.expires_at > time.monotonic():
                self._cache.move_to_end(user_id)
                return cached.identity
            try:
                async with asyncio.timeout(self._lookup_timeout):
                    user = await self._slack.get_user(user_id)
                identity = self._eligible_identity(user_id, user)
            except Exception as error:
                # Never log exception text: provider responses may contain emails
                # or credentials. Cancellation deliberately propagates.
                logger.warning("Slack identity lookup failed (%s)", type(error).__name__)
                identity = None
            ttl = self._cache_ttl if identity is not None else self._retry_seconds
            self._cache[user_id] = _CachedIdentity(time.monotonic() + ttl, identity)
            self._cache.move_to_end(user_id)
            while len(self._cache) > self._max_cache_entries:
                self._cache.popitem(last=False)
            return identity

    async def invalidate(self, user_id: str) -> None:
        """Expire a cached profile, for example after a verified Slack user_change event."""
        lock = self._locks.setdefault(user_id, asyncio.Lock())
        async with lock:
            self._cache.pop(user_id, None)

    def _eligible_identity(self, user_id: str, user: SlackUser) -> SlackIdentity | None:
        if (
            user.id != user_id
            or user.team_id != self._workspace_id
            or user.is_bot
            or user.deleted
            or user.is_guest
            or user.is_external
        ):
            return None
        email = user.email.strip().lower() if isinstance(user.email, str) else ""
        if not re.fullmatch(r"[^\s@]+@[^\s@]+", email):
            return None
        if email.rpartition("@")[2] not in self._domains:
            return None
        return SlackIdentity(
            workspace_id=self._workspace_id,
            user_id=user_id,
            email=email,
            display_name=user.display_name,
            checked_at=datetime.now(UTC),
        )
