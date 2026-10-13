import assert from "node:assert/strict";
import test from "node:test";
import type { SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import type { MessageContext } from "@berriai/agentchat";
import { createClaudeHandler, type ClaudeQuery } from "./claude-handler.ts";

const success = (session: string, result: string): SDKMessage => ({
  type: "result", subtype: "success", is_error: false, session_id: session, result,
} as SDKMessage);

function context(id: string, text = "hello", signal = new AbortController().signal): MessageContext {
  return { conversation: { id }, message: { text }, signal } as MessageContext;
}

test("resumes only the matching conversation and closes every query", async () => {
  const calls: Parameters<ClaudeQuery>[0][] = [];
  let closed = 0;
  const query: ClaudeQuery = (input) => {
    calls.push(input);
    return {
      async *[Symbol.asyncIterator]() { yield success(input.options.resume ?? `session-${calls.length}`, "answer"); },
      close() { closed++; },
    };
  };
  const handler = createClaudeHandler(query, { tools: [], permissionMode: "default" });
  assert.equal(await handler(context("thread-a")), "answer");
  assert.equal(await handler(context("thread-b")), "answer");
  assert.equal(await handler(context("thread-a", "follow up")), "answer");
  assert.deepEqual(calls.map((call) => call.options.resume), [undefined, undefined, "session-1"]);
  assert.equal(calls[2].prompt, "follow up");
  assert.equal(closed, 3);
});

test("failed or incomplete Claude results do not produce replies or replace sessions", async () => {
  let attempt = 0;
  const resumed: (string | undefined)[] = [];
  const handler = createClaudeHandler((input) => {
    resumed.push(input.options.resume);
    const current = ++attempt;
    return {
      async *[Symbol.asyncIterator]() {
        if (current === 1) yield success("good", "answer");
        else if (current === 2) yield { type: "result", subtype: "error_max_turns", is_error: true, session_id: "bad" } as SDKMessage;
      },
    };
  });
  await handler(context("a"));
  await assert.rejects(handler(context("a")), /did not return a successful answer/);
  await assert.rejects(handler(context("a")), /ended without a successful result/);
  assert.deepEqual(resumed, [undefined, "good", "good"]);
});

test("application cancellation aborts and closes the Claude query", async () => {
  const controller = new AbortController();
  let sdkAborted = false;
  let closed = false;
  let started!: () => void;
  const start = new Promise<void>((resolve) => { started = resolve; });
  const handler = createClaudeHandler(({ options }) => ({
    async *[Symbol.asyncIterator]() {
      await new Promise<void>((resolve) => {
        options.abortController!.signal.addEventListener("abort", () => { sdkAborted = true; resolve(); }, { once: true });
        started();
      });
      yield success("cancelled", "must not reply");
    },
    close() { closed = true; },
  }));
  const response = handler(context("a", "wait", controller.signal));
  await start;
  controller.abort();
  await assert.rejects(response, { name: "AbortError" });
  assert.equal(sdkAborted, true);
  assert.equal(closed, true);
});
