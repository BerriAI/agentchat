import assert from "node:assert/strict";
import test from "node:test";
import { AgentChat, Slack } from "@berriai/agentchat";
import { createClaudeHandler } from "./claude-handler.ts";
import { slackFixture, eventually } from "../../packages/node/test/slack-fixture.mjs";

test("Claude-style stream runs through the Rust Slack transport and resumes its conversation", { timeout: 10000 }, async () => {
  const fixture = await slackFixture();
  const calls = [];
  let closed = 0;
  const query = (input) => {
    calls.push(input);
    return {
      async *[Symbol.asyncIterator]() {
        yield { type: "assistant", message: { content: [{ type: "text", text: "intermediate, not a reply" }] } };
        yield { type: "result", subtype: "success", is_error: false, result: `Claude: ${input.prompt}`, session_id: "claude-session" };
      },
      close() { closed++; },
    };
  };
  const app = new AgentChat({ channels: [new Slack({ botToken: "xoxb-fixture", appToken: "xapp-fixture", apiBaseUrl: fixture.apiBaseUrl, ackEmoji: null })] });
  const handle = createClaudeHandler(query, { tools: [], permissionMode: "default" });
  app.onMessage(async (context) => {
    await context.subscribe();
    return handle(context);
  });
  const running = app.run();
  try {
    await fixture.send("<@B1> hello");
    await eventually(() => fixture.calls.filter((call) => call.method === "chat.postMessage").length === 1);
    await fixture.send("remember me", { ts: "100.2", type: "message" });
    await eventually(() => fixture.calls.filter((call) => call.method === "chat.postMessage").length === 2);
    assert.deepEqual(calls.map((call) => call.options.resume), [undefined, "claude-session"]);
    assert.equal(closed, 2);
    assert.deepEqual(fixture.calls.filter((call) => call.method === "chat.postMessage").map((call) => call.body.text), ["Claude: hello", "Claude: remember me"]);
  } finally {
    await app.close();
    await running;
    await fixture.close();
  }
});
