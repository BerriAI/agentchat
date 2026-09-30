import asyncio
import json

import pytest
from agents import Agent, Runner
from agents.items import ModelResponse
from agents.models.interface import Model, ModelTracing
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from agentchat import AgentChat, Message, Sender
from agentchat.channels import Slack
from agentchat.integrations import should_reply, to_openai_input
from agentchat.state import MemoryState


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


class FakeSlackClient:
    def __init__(self) -> None:
        self.posts: list[dict[str, object]] = []

    async def chat_postMessage(self, **kwargs):
        self.posts.append(kwargs)
        return {"ok": True, "ts": str(len(self.posts))}


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
        Message("2", "thread", "example", Sender("bot"), "Which period?", "assistant"),
        Message("3", "thread", "example", Sender("bob"), "Last quarter", "user"),
    )
    result = to_openai_input(messages, include_senders=True)
    assert [item["role"] for item in result] == ["user", "assistant", "user"]
    assert [json.loads(item["content"])["sender_id"] for item in result] == ["alice", "bot", "bob"]
    assert json.loads(result[-1]["content"])["text"] == "Last quarter"
    assert to_openai_input(messages)[0]["content"] == "Prepare a report"


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", [False, True])
async def test_reply_filter_uses_selected_model_without_tools_or_tracing(reply):
    class RoutingModel(StaticModel):
        async def get_response(self, *args, **kwargs):
            assert kwargs["tools"] == []
            assert kwargs["handoffs"] == []
            assert kwargs["output_schema"] is not None
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
