"""Provision a local profile on first Slack use, without a model API key.

This example's account directory is memory-only. Replace ``upsert_account``
with an atomic database upsert in a real application. Verified Google/SSO login
and matching to that application's existing accounts belong in that database.
"""

import asyncio
import os
from dataclasses import dataclass
from uuid import uuid4

from agentchat import AgentChat, MessageContext, SlackIdentity, SlackIdentityProvisioner
from agentchat.channels import Slack


@dataclass
class DemoAccount:
    id: str
    email: str
    needs_review: bool = False


accounts: dict[tuple[str, str, str], DemoAccount] = {}


async def upsert_account(identity: SlackIdentity) -> DemoAccount:
    # Stable provider IDs make repeated/concurrent first use idempotent here.
    # Production databases must enforce a unique key and transact this upsert.
    account = accounts.setdefault(identity.key, DemoAccount(str(uuid4()), identity.email))
    if account.email != identity.email:
        # Do not move past usage to a different person after an email change.
        account.needs_review = True
    return account


async def main() -> None:
    workspace_id = os.environ["SLACK_WORKSPACE_ID"]
    slack = Slack(
        bot_token=os.environ["SLACK_BOT_TOKEN"],
        app_token=os.environ["SLACK_APP_TOKEN"],
        workspace_id=workspace_id,
    )
    identities = SlackIdentityProvisioner(
        slack,
        workspace_id=workspace_id,
        allowed_email_domains=os.environ["SSO_EMAIL_DOMAINS"].split(","),
        on_identity=upsert_account,
    )
    app = AgentChat(channels=[slack])

    @app.on_message
    async def respond(context: MessageContext) -> str:
        # This demo permits every member of the configured workspace/domain.
        # Add any narrower application authorization check before provisioning.
        account = await identities.provision(context.message)
        if account is None:
            return "I could not identify an eligible company profile for this message."
        if account.needs_review:
            return "Your profile changed. An administrator needs to review its account link."
        await slack.subscribe(context.message)
        return "Your app profile is ready. Google sign-in can link it to your existing account."

    try:
        await app.run()
    finally:
        await app.close()


if __name__ == "__main__":
    asyncio.run(main())
