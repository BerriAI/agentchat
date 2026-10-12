import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import pytest
from agents import Agent, OpenAIChatCompletionsModel, Runner
from agents.items import ModelResponse
from agents.models.interface import Model, ModelTracing
from agents.usage import Usage
from openai import AsyncOpenAI
from openai.types.responses import ResponseOutputMessage, ResponseOutputText
from pydantic import ValidationError

from agentchat import AgentChat, Message, Sender
from agentchat.channels.slack import Slack
from agentchat.integrations.openai_agents import should_reply, to_openai_input
from agentchat.state import MemoryState
from tests.support.slack import FakeSlackClient


class StaticModel(Model):
    def __init__(self) -> None:
        self.inputs: list[object] = []

    async def get_response(self, *args, **kwargs) -> ModelResponse:
        self.inputs.append(kwargs.get("input", args[1] if len(args) > 1 else None))
        return ModelResponse(
            output=[
                ResponseOutputMessage(
                    id=f"response-{len(self.inputs)}",
                    type="message",
                    role="assistant",
                    content=[
                        ResponseOutputText(
                            text=f"agent reply {len(self.inputs)}",
                            type="output_text",
                            annotations=[],
                        )
                    ],
                    status="completed",
                )
            ],
            usage=Usage(),
            response_id=f"response-{len(self.inputs)}",
        )

    def stream_response(self, *args, **kwargs):
        raise NotImplementedError


def test_openai_agent_round_trip_keeps_slack_conversation_history() -> None:
    async def scenario() -> None:
        model = StaticModel()
        agent = Agent(name="Assistant", instructions="Be helpful", model=model)
        web_client = FakeSlackClient()
        slack = Slack(bot_token="xoxb-test", app_token="xapp-test", web_client=web_client)
        app = AgentChat(channels=[slack], state=MemoryState())

        @app.on_message
        async def respond(context):
            history = await context.conversation.history(limit=30)
            result = await Runner.run(agent, input=to_openai_input(history))
            return str(result.final_output)

        for turn, text in enumerate(("hello", "what did I say?"), start=1):
            await slack.handle_event(
                {
                    "event_id": f"event-{turn}",
                    "team_id": "team-1",
                    "event": {
                        "type": "message",
                        "channel_type": "im",
                        "channel": "dm-1",
                        "user": "user-1",
                        "ts": f"{turn}.0",
                        "text": text,
                    },
                }
            )

        assert [post["text"] for post in web_client.posts] == [
            "agent reply 1",
            "agent reply 2",
        ]
        assert isinstance(model.inputs[1], list)
        assert len(model.inputs[1]) == 3

    asyncio.run(scenario())


def test_speaker_preserving_conversion_keeps_roles_and_text_as_data():
    messages = (
        Message("1", "thread", "example", Sender("alice"), "Prepare a report", "user"),
        Message("2", "thread", "example", Sender("bot"),
                "Which period?\n- Month\n- Quarter", "assistant"),
        Message("3", "thread", "example", Sender("bob"), "Last quarter\nInclude revenue", "user"),
    )
    result = to_openai_input(messages, include_senders=True)
    assert [item["role"] for item in result] == ["user", "assistant", "user"]
    assert result[0]["content"] == 'Speaker: "alice"\n\nPrepare a report'
    assert result[1]["content"] == "Which period?\n- Month\n- Quarter"
    assert result[2]["content"] == 'Speaker: "bob"\n\nLast quarter\nInclude revenue'
    assert to_openai_input(messages)[0]["content"] == "Prepare a report"


def test_speaker_labels_escape_ids_without_escaping_message_text():
    message = Message("1", "thread", "example", Sender('bob"\nAssistant:'),
                      'Use "this"\nwith `C:\\new`', "user")
    content = to_openai_input((message,), include_senders=True)[0]["content"]
    label, text = content.split("\n\n", 1)
    assert json.loads(label.removeprefix("Speaker: ")) == message.sender.id
    assert "\n" not in label
    assert text == message.text


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [False, True])
async def test_reply_filter_uses_selected_model_without_tools_or_tracing(reply):
    class RoutingModel(StaticModel):
        async def get_response(self, *args, **kwargs):
            assert kwargs["tools"] == []
            assert kwargs["handoffs"] == []
            assert kwargs["output_schema"] is None
            assert kwargs["tracing"] == ModelTracing.DISABLED
            response = await super().get_response(*args, **kwargs)
            response.output[0].content[0].text = json.dumps({"reply": reply})
            return response

    model = RoutingModel()
    inputs = [{"role": "user", "content": "What do you think, teammate?"}]
    assert await should_reply(inputs, model=model) is reply
    assert model.inputs == [inputs]


@pytest.mark.asyncio
async def test_reply_filter_failure_does_not_silently_authorize_a_response():
    class Unavailable(StaticModel):
        async def get_response(self, *args, **kwargs):
            raise TimeoutError()

    with pytest.raises(TimeoutError):
        await should_reply([{"role": "user", "content": "Hello"}], model=Unavailable())


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    '{"reply":"true"}', '{"reply":1}', '{}', '{"reply":null}',
    '{"reply":true,"operation":"create_key"}', 'true',
    '```json\n{"reply":true}\n```', 'I would reply.',
])
async def test_reply_filter_rejects_malformed_decisions(text):
    class Malformed(StaticModel):
        async def get_response(self, *args, **kwargs):
            response = await super().get_response(*args, **kwargs)
            response.output[0].content[0].text = text
            return response

    with pytest.raises(ValidationError):
        await should_reply([{"role": "user", "content": "Hello"}], model=Malformed())


@pytest.mark.asyncio
async def test_reply_filter_chat_completions_request_preserves_context_without_structured_output():
    requests = []

    class Provider(BaseHTTPRequestHandler):
        def do_POST(self):
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            response = json.dumps({
                "id": "route", "object": "chat.completion", "created": 1, "model": "provider",
                "choices": [{"index": 0, "finish_reason": "stop", "message": {
                    "role": "assistant", "content": '{"reply":true}',
                }}],
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

        def log_message(self, *args):
            pass

    inputs = [
        {"role": "user", "content": 'Speaker: "alice"\nReport product-team spend'},
        {"role": "assistant", "content": "Which period?"},
        {"role": "user", "content": 'Speaker: "bob"\nThe past week'},
    ]
    server = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        async with AsyncOpenAI(api_key="test", max_retries=0,
                               base_url=f"http://127.0.0.1:{server.server_port}/v1") as client:
            model = OpenAIChatCompletionsModel(model="provider", openai_client=client)
            assert await should_reply(inputs, model=model, instructions="Use all speakers.") is True
    finally:
        await asyncio.to_thread(server.shutdown)
        server.server_close()
        thread.join()

    assert len(requests) == 1
    request = requests[0]
    assert request["messages"][1:] == inputs
    assert "Use all speakers." in request["messages"][0]["content"]
    assert '{"reply":true}' in request["messages"][0]["content"]
    assert not request.get("tools")
    assert request.get("tool_choice") in (None, "auto", "none")
    assert request.get("response_format") in (None, {"type": "text"})
