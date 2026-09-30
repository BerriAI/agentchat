from dataclasses import replace

import pytest
from test_app import FakeChannel
from test_slack import FakeSlackClient

from agentchat import AgentChat, Message, Sender
from agentchat.channels import Slack
from agentchat.channels.slack_mirror import mirror_payload


def source(thread='1.0'):
    return Message(id='slack:event', conversation_id='slack:T:C:1.0', channel='slack',
                   sender=Sender('U'), text='Start', role='user', thread_id=thread,
                   metadata={'team_id': 'T', 'channel_id': 'C',
                             'reply_thread_timestamp': thread, 'message_timestamp': '9.0'})


def external(text='Continue with <!channel> and <@U> & code'):
    return Message(id='web:123', conversation_id='untrusted-destination', channel='web',
                   sender=Sender('google:alice', 'Alice'), text=text, role='user',
                   metadata={'channel_id': 'DO-NOT-USE', 'reply_thread_timestamp': 'other'})


@pytest.mark.asyncio
@pytest.mark.parametrize('thread', [None, '1.0'])
async def test_mirror_routes_to_saved_source_preserves_user_and_never_invokes_agent(thread):
    client = FakeSlackClient()
    slack = Slack(bot_token='test', app_token='test', web_client=client, workspace_id='T')
    app = AgentChat(channels=[slack])

    @app.on_message
    async def unexpected(context):
        raise AssertionError('Mirroring must not execute another turn')

    message = external()
    sent = await app.mirror(slack, source(thread), message, origin='Moyai web')
    post = client.posts[0]
    assert post['channel'] == 'C' and post['thread_ts'] == thread
    assert post['blocks'][0]['elements'][0]['text'] == 'Alice · via Moyai web'
    assert post['blocks'][1]['text'] == {
        'type': 'plain_text', 'text': message.text, 'emoji': False,
    }
    assert '<!channel>' not in post['text'] and '<@U>' not in post['text']
    assert not post['mrkdwn'] and not post['link_names'] and not post['unfurl_links']
    assert post['parse'] == 'none'
    assert sent.role == 'user' and sent.sender == message.sender and sent.text == message.text
    assert sent.conversation_id == source().conversation_id
    assert await app.state.history(source().conversation_id) == (sent,)
    await slack.handle_event({'team_id': 'T', 'event_id': 'echo', 'event': {
        'type': 'message', 'channel_type': 'im', 'channel': 'C', 'user': 'BOT',
        'bot_id': 'B', 'ts': '2.0', **post,
    }})
    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_native_history_restores_mirror_role_after_restart_but_rejects_spoofed_metadata():
    message = external()
    record = mirror_payload(message, origin='Moyai web')

    class Client(FakeSlackClient):
        async def conversations_replies(self, **kwargs):
            assert kwargs['include_all_metadata'] is True
            return {'ok': True, 'messages': [
                {'user': 'U', 'ts': '1.0', 'text': 'Start'},
                {**record, 'user': 'BOT', 'ts': '2.0'},
                {**record, 'user': 'SPOOFER', 'ts': '3.0'},
                {'user': 'BOT', 'ts': '4.0', 'text': 'Answer'},
            ]}

    slack = Slack(bot_token='test', app_token='test', web_client=Client(),
                  workspace_id='T', bot_user_id='BOT')
    history = await slack.thread_history(source())
    assert history[1].sender == message.sender
    assert history[1].role == 'user' and history[1].text == message.text
    assert history[2].sender.id == 'SPOOFER' and history[2].text != message.text
    assert history[3].role == 'assistant'


@pytest.mark.asyncio
async def test_rejects_invalid_mirror_without_posting_and_preserves_existing_channels():
    channel = FakeChannel()
    app = AgentChat(channels=[channel])
    with pytest.raises(TypeError, match='does not support'):
        await app.mirror(channel, source(), external())
    client = FakeSlackClient()
    slack = Slack(bot_token='test', app_token='test', web_client=client, workspace_id='T')
    for message in (replace(external(), role='assistant'), external('x' * 2801)):
        with pytest.raises(ValueError):
            await slack.mirror(source(), message)
    with pytest.raises(ValueError, match='workspace'):
        await slack.mirror(replace(source(), metadata={'team_id': 'OTHER'}), external())
    assert client.posts == []


@pytest.mark.asyncio
async def test_ambiguous_delivery_is_propagated_without_retry_or_history_append():
    class Client(FakeSlackClient):
        async def chat_postMessage(self, **kwargs):
            self.posts.append(kwargs)
            raise TimeoutError('Response lost')

    client = Client()
    slack = Slack(bot_token='test', app_token='test', web_client=client)
    app = AgentChat(channels=[slack])
    with pytest.raises(TimeoutError):
        await app.mirror(slack, source(), external())
    assert len(client.posts) == 1
    assert not await app.state.history(source().conversation_id)
