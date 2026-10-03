from dataclasses import replace

import pytest
from test_mirroring import source
from test_slack import FakeSlackClient

from agentchat import AgentChat, RichReply, UploadFile
from agentchat.channels import Slack, slack_media


def destination(thread: str | None = '1.0'):
    message = source(thread)
    return replace(message, metadata={**message.metadata, 'channel_id': 'C123'})


@pytest.mark.asyncio
@pytest.mark.parametrize('thread', [None, '1.0'])
async def test_native_slack_upload_and_rich_reply_share_saved_routing(monkeypatch, thread):
    calls = []

    class Client(FakeSlackClient):
        async def api_call(self, api_method, **kwargs):
            calls.append((api_method, kwargs))
            assert kwargs['http_verb'] == 'POST'
            if api_method == 'files.getUploadURLExternal':
                assert kwargs['data'] == {'filename': 'demo.webm', 'length': 5}
                return {'ok': True, 'file_id': 'F1',
                        'upload_url': 'https://files.slack.com/upload/F1'}
            return {'ok': True, 'files': [{'id': 'F1'}]}

    uploaded = []

    async def upload(url: str, raw: bytes) -> None:
        uploaded.append((url, raw))

    monkeypatch.setattr(slack_media, 'upload_bytes', upload)
    client = Client()
    slack = Slack(bot_token='test', app_token='test', web_client=client, workspace_id='T')
    app = AgentChat(channels=[slack])
    message = destination(thread)
    files = await app.upload_files(slack, message, [UploadFile('demo.webm', b'video')])
    assert [file.id for file in files] == ['F1']
    assert uploaded == [('https://files.slack.com/upload/F1', b'video')]
    assert calls[-1][1]['json'] == {'files': [{'id': 'F1', 'title': 'demo.webm'}],
                                  'channel_id': 'C123', **({'thread_ts': thread} if thread else {})}
    assert await app.state.history(message.conversation_id) == ()
    reply = RichReply(text='Verified demo', attachments=({'color': '#5B3FD1', 'text': 'PR'},))
    sent = await app.reply_rich(slack, message, reply)
    assert client.posts[0]['channel'] == 'C123' and client.posts[0]['thread_ts'] == thread
    assert client.posts[0]['attachments'] == [{'color': '#5B3FD1', 'text': 'PR'}]
    assert sent.text == 'Verified demo' and sent.metadata['message_timestamp'] == '1'
    assert await app.state.history(message.conversation_id) == (sent,)


@pytest.mark.asyncio
async def test_rich_slack_reply_requires_valid_source_and_provider_ack():
    class Client(FakeSlackClient):
        async def chat_postMessage(self, **kwargs):
            self.posts.append(kwargs)
            return {'ok': True}

    client = Client()
    slack = Slack(bot_token='test', app_token='test', web_client=client, workspace_id='T')
    app = AgentChat(channels=[slack])
    message = destination()
    for bad in (replace(message, channel='web'),
                replace(message, metadata={**message.metadata, 'team_id': 'OTHER'}),
                replace(message, metadata={**message.metadata, 'channel_id': 'https://example.com'})):
        with pytest.raises(ValueError):
            await app.reply_rich(slack, bad, RichReply('Result'))
    assert client.posts == []
    with pytest.raises(RuntimeError, match='could not be confirmed'):
        await app.reply_rich(slack, message, RichReply('Result'))
    assert await app.state.history(message.conversation_id) == ()
    assert len(client.posts) == 1


@pytest.mark.asyncio
async def test_native_upload_exercises_real_slack_client_request_builder(monkeypatch):
    # Intercept only HTTP I/O so parameter/API drift at the dependency floor is visible.
    slack = Slack(bot_token='test-only', app_token='test-only', workspace_id='T')
    calls = []

    async def send(http_verb: str, api_url: str, req_args: dict) -> dict:
        assert http_verb == 'POST'
        calls.append(api_url)
        if api_url.endswith('files.getUploadURLExternal'):
            assert req_args['data'] == {'filename': 'demo.webm', 'length': 5}
            return {'ok': True, 'file_id': 'F1',
                    'upload_url': 'https://files.slack.com/upload/F1'}
        assert api_url.endswith('files.completeUploadExternal')
        assert req_args['json']['channel_id'] == 'C123'
        assert req_args['json']['files'] == [{'id': 'F1', 'title': 'demo.webm'}]
        return {'ok': True, 'files': [{'id': 'F1'}]}

    async def upload(url: str, raw: bytes) -> None:
        assert url == 'https://files.slack.com/upload/F1' and raw == b'video'

    monkeypatch.setattr(slack._web_client, '_send', send)
    monkeypatch.setattr(slack_media, 'upload_bytes', upload)
    assert slack._web_client.retry_handlers == []
    files = await slack.upload_files(destination(), [UploadFile('demo.webm', b'video')])
    assert [file.id for file in files] == ['F1'] and len(calls) == 2
