import { query } from "@anthropic-ai/claude-agent-sdk";
import { AgentChat, Slack } from "@berriai/agentchat";
import { createClaudeHandler } from "./claude-handler.ts";

const app = new AgentChat({ channels: [Slack.fromEnv()] });
const handle = createClaudeHandler(query, {
  // A chat-only example. Enable tools deliberately with your own approval policy.
  tools: [],
  permissionMode: "default",
  ...(process.env.CLAUDE_MODEL ? { model: process.env.CLAUDE_MODEL } : {}),
});
app.onMessage(async (context) => {
  // This demo accepts users able to reach the bot. Apply your authorization
  // policy here before subscribing, reading history, or invoking Claude.
  await context.subscribe();
  return handle(context);
});

const stop = () => { void app.close().catch(console.error); };
process.once("SIGINT", stop);
process.once("SIGTERM", stop);
try {
  await app.run();
} finally {
  await app.close();
  process.removeListener("SIGINT", stop);
  process.removeListener("SIGTERM", stop);
}
