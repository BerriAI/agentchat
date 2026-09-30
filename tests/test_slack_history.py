import pytest

from agentchat import Message, Sender
from agentchat.channels import Slack


def source(ts="9.0", team="T123"):
    return Message(id="current", conversation_id="thread", channel="slack",
                   sender=Sender("Usecond"), text="target@example.com", role="user",
                   metadata={"team_id": team, "channel_id": "C123",
                             "reply_thread_timestamp": "1.0", "message_timestamp": ts})


@pytest.mark.asyncio
async def test_history_keeps_root_recent_messages_and_speakers_across_pages():
    class Client:
        def __init__(self):
            self.calls = []

        async def conversations_replies(self, **kwargs):
            self.calls.append(kwargs)
            if not kwargs["cursor"]:
                return {"ok": True, "has_more": True, "response_metadata": {"next_cursor": "next"},
                        "messages": [{"ts": "1.0", "user": "Ufirst", "text": "Create a key"},
                                     {"ts": "2.0", "user": "BOT", "text": "Which user?"}]}
            return {"ok": True, "has_more": False, "messages": [
                {"ts": "3.0", "user": "Usecond", "text": "target@example.com"},
                {"ts": "4.0", "user": "BOT", "text": "Created it"},
                {"ts": "9.0", "user": "Usecond", "text": "current, excluded"},
                {"ts": "10.0", "user": "Ufirst", "text": "future, excluded"},
            ]}

    client = Client()
    slack = Slack(bot_token="test", app_token="test", web_client=client,
                  workspace_id="T123", bot_user_id="BOT")
    history = await slack.thread_history(source(), limit=3)
    assert [(m.sender.id, m.role, m.text) for m in history] == [
        ("Ufirst", "user", "Create a key"),
        ("Usecond", "user", "target@example.com"),
        ("BOT", "assistant", "Created it"),
    ]
    assert [call["cursor"] for call in client.calls] == ["", "next"]
    assert all(call["channel"] == "C123" and call["ts"] == "1.0"
               and call["latest"] == "9.0" and not call["inclusive"] for call in client.calls)


@pytest.mark.asyncio
async def test_new_threads_and_wrong_workspaces_never_fetch_history():
    slack = Slack(bot_token="test", app_token="test", web_client=object(), workspace_id="T123")
    assert await slack.thread_history(source("1.0")) == ()
    with pytest.raises(ValueError, match="workspace"):
        await slack.thread_history(source(team="TOTHER"))


@pytest.mark.asyncio
@pytest.mark.parametrize("cursor", [None, "repeated"])
async def test_incomplete_history_fails_instead_of_silently_losing_context(cursor):
    class Client:
        async def conversations_replies(self, **kwargs):
            return {"ok": True, "messages": [], "has_more": True,
                    "response_metadata": {"next_cursor": cursor}}

    slack = Slack(bot_token="test", app_token="test", web_client=Client())
    with pytest.raises(ValueError, match="pagination"):
        await slack.thread_history(source())
