# agentchat
Unified SDK to connect your agent to Slack, Email, Teams. Focus on your core agent not the communication channels. 

## Quick Start
#### 1. Define your agent 
```python
from agentchat import AgentChat
from agentchat.channels import Slack, Discord
from agentchat.state import RedisState

chat_app = AgentChat(
    channels=[
        Slack.from_env(
            respond_to={"dm", "mention"},
            follow_threads=True,
        ),
        Discord.from_env(
            respond_to={"dm", "mention"},
            follow_threads=True,
        ),
    ],
    state=RedisState.from_env(),
)
````

### 2. Connect your Agent to Agent Chat

```python
from agents import Agent, Runner

agent = Agent(
    name="Assistant",
    instructions="You are a helpful assistant",
)


@app.on_message
async def respond(context):
    history = await context.conversation.history(limit=30)
    result = Runner.run_streamed(agent, input=to_openai_input(history))

    return result.text_stream()
```
