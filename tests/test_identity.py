import asyncio
from dataclasses import replace
from types import MappingProxyType

import pytest
from slack_sdk.socket_mode.request import SocketModeRequest

from agentchat import AgentChat, Message, Sender, SlackIdentityProvisioner
from agentchat.channels import Slack, SlackUser


def message(user="U12345678", *, team="T12345678", **overrides):
    return Message(**{
        "id": "message-1", "conversation_id": "conversation-1", "channel": "slack",
        "sender": Sender(id=user), "role": "user", "text": "Please help",
        "metadata": MappingProxyType({"team_id": team}),
    } | overrides)


class Profiles:
    def __init__(self):
        self.calls = []
        self.user = SlackUser("U12345678", "T12345678", "Alice", " Alice@BERRI.AI ", False, False)
        self.error = None

    async def get_user(self, user_id):
        self.calls.append(user_id)
        if self.error:
            raise self.error
        return self.user


def provisioner(profiles, callback, **kwargs):
    return SlackIdentityProvisioner(
        profiles, workspace_id="T12345678", allowed_email_domains={"berri.ai"},
        on_identity=callback, **kwargs,
    )


def test_first_use_calls_app_upsert_and_preserves_sender_and_message():
    async def scenario():
        profiles = Profiles()
        accounts, seen = {}, []

        async def upsert(identity):
            seen.append(identity)
            return accounts.setdefault(identity.key, {"id": "app-account", "email": identity.email})

        identities = provisioner(profiles, upsert)
        original = message()
        first = await identities.provision(original)
        second = await identities.provision(original)
        assert first is second
        assert first == {"id": "app-account", "email": "alice@berri.ai"}
        assert seen[0].key == ("slack", "T12345678", "U12345678")
        assert seen[0].display_name == "Alice"
        assert seen[0].checked_at.tzinfo is not None
        assert profiles.calls == ["U12345678"]
        assert original.sender == Sender(id="U12345678")
        assert original.metadata == {"team_id": "T12345678"}
        assert len(accounts) == 1

    asyncio.run(scenario())


def test_callback_can_pick_up_later_sso_link_without_another_slack_lookup():
    async def scenario():
        profiles = Profiles()
        linked_account = "pending-account"

        async def upsert(identity):
            return linked_account

        identities = provisioner(profiles, upsert)
        assert await identities.provision(message()) == "pending-account"
        linked_account = "verified-google-account"
        assert await identities.provision(message()) == "verified-google-account"
        assert profiles.calls == ["U12345678"]

    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"id": "U87654321"}, {"team_id": "T87654321"}, {"is_bot": True}, {"deleted": True},
    {"is_guest": True}, {"is_external": True}, {"email": None}, {"email": ""},
    {"email": "alice@gmail.com"}, {"email": "alice@evilberri.ai"},
    {"email": "alice@berri.ai.evil.example"}, {"email": "alice@@berri.ai"},
    {"email": "Alice <alice@berri.ai>"},
])
def test_ineligible_profiles_cannot_create_accounts(changes):
    async def scenario():
        profiles = Profiles()
        profiles.user = replace(profiles.user, **changes)

        async def forbidden(identity):
            pytest.fail("Ineligible profile reached account callback")

        identities = provisioner(profiles, forbidden)
        assert await identities.provision(message()) is None
        assert await identities.provision(message()) is None
        assert len(profiles.calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("incoming", [
    message(team="another-team"), message(user=""), message(channel="discord"),
    message(role="assistant"), message(metadata={}),
])
def test_wrong_workspace_or_non_user_message_does_not_fetch_profile(incoming):
    async def scenario():
        profiles = Profiles()

        async def forbidden(identity):
            pytest.fail("Invalid sender reached account callback")

        assert await provisioner(profiles, forbidden).provision(incoming) is None
        assert not profiles.calls

    asyncio.run(scenario())


def test_email_aliases_are_not_collapsed_and_keys_survive_email_changes():
    async def scenario():
        profiles = Profiles()
        seen = []

        async def upsert(identity):
            seen.append(identity)

        identities = provisioner(profiles, upsert)
        profiles.user = replace(profiles.user, email="A.Lice+one@BERRI.AI")
        await identities.provision(message())
        profiles.user = replace(profiles.user, email="bob@berri.ai")
        await identities.invalidate("U12345678")
        await identities.provision(message())
        assert [identity.email for identity in seen] == ["a.lice+one@berri.ai", "bob@berri.ai"]
        assert seen[0].key == seen[1].key
        assert len(profiles.calls) == 2

    asyncio.run(scenario())


def test_failures_are_bounded_cached_and_do_not_leak_provider_data(monkeypatch, caplog):
    async def scenario():
        profiles = Profiles()
        profiles.error = RuntimeError("secret-token alice@berri.ai")
        clock = [0.0]
        monkeypatch.setattr("agentchat.identity.time.monotonic", lambda: clock[0])

        async def upsert(identity):
            return identity.key

        identities = provisioner(profiles, upsert, retry_seconds=10)
        assert await identities.provision(message()) is None
        assert await identities.provision(message()) is None
        assert len(profiles.calls) == 1
        clock[0] = 11
        profiles.error = None
        assert await identities.provision(message()) == ("slack", "T12345678", "U12345678")
        assert len(profiles.calls) == 2
        assert "secret-token" not in caplog.text and "alice@berri.ai" not in caplog.text

    asyncio.run(scenario())


def test_expired_profiles_do_not_fall_back_to_stale_emails(monkeypatch):
    async def scenario():
        profiles = Profiles()
        clock = [0.0]
        monkeypatch.setattr("agentchat.identity.time.monotonic", lambda: clock[0])

        async def upsert(identity):
            return identity.email

        identities = provisioner(profiles, upsert, cache_ttl_seconds=10)
        assert await identities.provision(message()) == "alice@berri.ai"
        clock[0] = 11
        profiles.error = RuntimeError("missing_scope")
        assert await identities.provision(message()) is None
        profiles.error = None
        profiles.user = replace(profiles.user, deleted=True)
        await identities.invalidate("U12345678")
        assert await identities.provision(message()) is None

    asyncio.run(scenario())


def test_concurrent_requests_share_lookup_and_cache_is_bounded():
    async def scenario():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []

        class SlowProfiles:
            async def get_user(self, user_id):
                calls.append(user_id)
                started.set()
                await release.wait()
                return SlackUser(user_id, "T12345678", user_id, "alice@berri.ai", False, False)

        async def upsert(identity):
            return identity.key

        identities = provisioner(SlowProfiles(), upsert, max_cache_entries=2)
        tasks = [asyncio.create_task(identities.provision(message())) for _ in range(5)]
        await started.wait()
        release.set()
        results = await asyncio.gather(*tasks)
        assert len(calls) == 1 and len(set(results)) == 1
        await identities.provision(message(user="second"))
        await identities.provision(message(user="third"))
        await identities.provision(message())
        assert calls == ["U12345678", "second", "third", "U12345678"]

    asyncio.run(scenario())


def test_timeout_returns_none_but_cancellation_and_account_store_errors_propagate():
    async def scenario():
        class SlowProfiles:
            async def get_user(self, user_id):
                await asyncio.Event().wait()

        async def upsert(identity):
            raise OSError("Database unavailable")

        identities = provisioner(SlowProfiles(), upsert, lookup_timeout_seconds=.01)
        assert await identities.provision(message()) is None
        identities = provisioner(SlowProfiles(), upsert)
        task = asyncio.create_task(identities.provision(message()))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        identities = provisioner(Profiles(), upsert)
        with pytest.raises(OSError, match="Database unavailable"):
            await identities.provision(message())

    asyncio.run(scenario())


def test_socket_ack_and_unauthorized_senders_do_not_wait_for_identity_lookup():
    async def scenario():
        calls, acknowledgements = [], []
        started, release = asyncio.Event(), asyncio.Event()

        class Client:
            async def users_info(self, *, user):
                calls.append(user)
                started.set()
                await release.wait()
                return {"ok": True, "user": {"id": user, "team_id": "T12345678", "is_bot": False,
                        "deleted": False, "profile": {"email": "alice@berri.ai"}}}

        class Socket:
            async def send_socket_mode_response(self, response):
                acknowledgements.append(response.envelope_id)

        async def upsert(identity):
            return identity.email

        slack = Slack(bot_token="test", app_token="test", web_client=Client(), ack_emoji=None)
        identities = provisioner(slack, upsert)
        app = AgentChat(channels=[slack])
        seen = []

        @app.on_message
        async def respond(context):
            if context.sender.id != "U12345678":
                return None
            seen.append(await identities.provision(context.message))

        def event(user, stamp):
            return {"team_id": "T12345678", "event": {"type": "message", "channel_type": "im",
                    "channel": "D12345678", "user": user, "ts": stamp, "text": "hello"}}

        await slack.handle_event(event("unauthorized", "1.0"))
        assert not calls
        payload = event("U12345678", "2.0")
        request = SocketModeRequest(type="events_api", envelope_id="accepted", payload=payload)
        await slack._handle_socket_request(Socket(), request)
        assert acknowledgements == ["accepted"]
        await started.wait()
        assert not seen
        release.set()
        await slack.close()
        assert seen == ["alice@berri.ai"]
        history = await app.state.history("slack:T12345678:D12345678")
        assert history[-1].sender.id == "U12345678"

    asyncio.run(scenario())


@pytest.mark.parametrize("options", [
    {"workspace_id": ""}, {"allowed_email_domains": []}, {"allowed_email_domains": "berri.ai"},
    {"allowed_email_domains": ["*.berri.ai"]}, {"lookup_timeout_seconds": 0},
    {"retry_seconds": float("nan")}, {"cache_ttl_seconds": -1}, {"max_cache_entries": 0},
])
def test_invalid_configuration_is_rejected(options):
    with pytest.raises(ValueError):
        SlackIdentityProvisioner(Profiles(), **{
            "workspace_id": "T12345678", "allowed_email_domains": {"berri.ai"},
            "on_identity": None,
        } | options)
