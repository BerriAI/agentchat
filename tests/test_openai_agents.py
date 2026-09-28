import asyncio

from agents import Agent, Runner
from agents.items import ModelResponse
from agents.models.interface import Model
from agents.usage import Usage
from openai.types.responses import ResponseOutputMessage, ResponseOutputText

from agentchat import AgentChat
from agentchat.channels import Slack
from agentchat.integrations import to_openai_input
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
