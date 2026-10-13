import assert from "node:assert/strict";
import test from "node:test";
import { AgentChat, Slack } from "../dist/index.js";
import { slackFixture, eventually } from "./slack-fixture.mjs";

test("native Slack dispatch supports reentrant reply/history, deduplication and thread subscription", { timeout: 10000 }, async () => {
  const fixture = await slackFixture();
  const app = new AgentChat({ channels: [new Slack({ botToken: "xoxb-fixture", appToken: "xapp-fixture", apiBaseUrl: fixture.apiBaseUrl })] });
  const contexts = [];
  let running;
  try {
    app.onMessage(async (context) => {
      contexts.push(context);
      const history = await context.conversation.history();
      assert.equal(history.at(-1).id, context.message.id);
      assert.equal((await context.history()).at(-1).id, context.message.id);
      await context.subscribe();
      if (contexts.length === 1) {
        assert.throws(() => { context.sourceJson = '{"channel":"forged"}'; }, TypeError);
        assert.throws(() => { context.message.metadata.channel_id = "forged"; }, TypeError);
        await context.reply("manual");
      }
      return `answer:${context.message.text}`;
    });
    running = app.run();
    void running.catch(() => {});
    await fixture.send("<@B1> hello");
    await eventually(() => fixture.calls.filter((call) => call.method === "chat.postMessage").length === 2);
    await fixture.send("<@B1> hello", { envelope: "redelivery" });
    await fixture.send("follow up", { ts: "100.2", type: "message" });
    await eventually(() => fixture.calls.filter((call) => call.method === "chat.postMessage").length === 3);
    assert.equal(contexts.length, 2);
    assert.equal(contexts[0].conversation.id, contexts[1].conversation.id);
    assert.deepEqual(fixture.calls.filter((call) => call.method === "chat.postMessage").map((call) => call.body.text), ["manual", "answer:hello", "answer:follow up"]);
    await eventually(async () => (await contexts[0].conversation.history()).length === 5);
    assert(fixture.calls.filter((call) => call.method === "chat.postMessage").every((call) => call.body.channel === "C1"));
    await eventually(() => fixture.acknowledgements.length === 3);
  } finally {
    try { await app.close(); await running; } finally { await fixture.close(); }
  }
});

test("close aborts in-flight host handlers and is idempotent", { timeout: 10000 }, async () => {
  const fixture = await slackFixture();
  const app = new AgentChat({ channels: [new Slack({ botToken: "xoxb-fixture", appToken: "xapp-fixture", apiBaseUrl: fixture.apiBaseUrl, shutdownTimeoutMs: 100 })] });
  let observedAbort = false;
  let started;
  const start = new Promise((resolve) => { started = resolve; });
  app.onMessage(async (context) => {
    started();
    await new Promise((resolve) => context.signal.addEventListener("abort", () => { observedAbort = true; resolve(); }, { once: true }));
    return "must not send after cancellation";
  });
  const running = app.run();
  void running.catch(() => {});
  try {
    await fixture.send("<@B1> wait");
    await start;
    await Promise.all([app.close(), app.close()]);
    await running;
    assert.equal(observedAbort, true);
    assert.equal(fixture.calls.filter((call) => call.method === "chat.postMessage").length, 0);
  } finally {
    await app.close();
    await fixture.close();
  }
});

test("external close drains an admitted handler and permits its final reply", { timeout: 10000 }, async () => {
  const fixture = await slackFixture();
  const app = new AgentChat({ channels: [new Slack({ botToken: "xoxb-fixture", appToken: "xapp-fixture", apiBaseUrl: fixture.apiBaseUrl, shutdownTimeoutMs: 1000 })] });
  let release;
  const gate = new Promise((resolve) => { release = resolve; });
  let started;
  const start = new Promise((resolve) => { started = resolve; });
  app.onMessage(async (context) => {
    started();
    await gate;
    assert.equal(context.signal.aborted, false);
    await context.reply("final admitted reply");
  });
  const running = app.run();
  void running.catch(() => {});
  try {
    await fixture.send("<@B1> finish");
    await start;
    const closing = app.close();
    release();
    await closing;
    await running;
    assert.deepEqual(fixture.calls.filter((call) => call.method === "chat.postMessage").map((call) => call.body.text), ["final admitted reply"]);
  } finally {
    release();
    try { await app.close(); await running; } finally { await fixture.close(); }
  }
});

test("a handler can await close without waiting for itself", { timeout: 10000 }, async () => {
  const fixture = await slackFixture();
  const app = new AgentChat({ channels: [new Slack({ botToken: "xoxb-fixture", appToken: "xapp-fixture", apiBaseUrl: fixture.apiBaseUrl, shutdownTimeoutMs: 4000 })] });
  let closeElapsed = Infinity;
  app.onMessage(async () => {
    const start = Date.now();
    await app.close();
    closeElapsed = Date.now() - start;
    return "closing gracefully";
  });
  const running = app.run();
  void running.catch(() => {});
  try {
    await fixture.send("<@B1> stop");
    await running;
    assert(closeElapsed < 500, `close from handler took ${closeElapsed}ms`);
    assert.deepEqual(fixture.calls.filter((call) => call.method === "chat.postMessage").map((call) => call.body.text), ["closing gracefully"]);
  } finally {
    try { await app.close(); } finally { await fixture.close(); }
  }
});

test("handler failure is observable, sends no reply, and does not stop the next turn", { timeout: 10000 }, async () => {
  const fixture = await slackFixture();
  const errors = [];
  const app = new AgentChat({ channels: [new Slack({ botToken: "xoxb-fixture", appToken: "xapp-fixture", apiBaseUrl: fixture.apiBaseUrl })], onError: (error) => errors.push(error) });
  app.onMessage(async (context) => {
    if (context.message.text === "fail") throw new Error("intentional handler failure");
    return "recovered";
  });
  const running = app.run();
  void running.catch(() => {});
  try {
    await fixture.send("<@B1> fail");
    await eventually(() => errors.length === 1);
    assert.match(errors[0].message, /intentional handler failure/);
    assert.equal(fixture.calls.filter((call) => call.method === "chat.postMessage").length, 0);
    await fixture.send("<@B1> recover", { ts: "100.2" });
    await eventually(() => fixture.calls.filter((call) => call.method === "chat.postMessage").length === 1);
  } finally {
    try { await app.close(); await running; } finally { await fixture.close(); }
  }
});

test("rich replies, status guards, explicit file transfer and mirroring use native capabilities", { timeout: 10000 }, async () => {
  const fixture = await slackFixture();
  let finished;
  let failed;
  const complete = new Promise((resolve, reject) => { finished = resolve; failed = reject; });
  const app = new AgentChat({
    channels: [new Slack({ botToken: "xoxb-fixture", appToken: "xapp-fixture", apiBaseUrl: fixture.apiBaseUrl })],
    onError: failed,
  });
  app.onMessage(async (context) => {
    const working = await context.working({ refreshIntervalMs: 100 });
    try {
      await working.update("reading file");
      const file = await context.downloadAttachment(context.message.attachments[0]);
      assert.equal(file.content.toString(), "hello");
      assert.equal(file.mediaType, "text/plain");
      await assert.rejects(context.downloadAttachment({ id: "FOTHER", name: null, size: null, mediaType: null }), /does not belong/);
      const uploads = await context.uploadFiles([{ filename: "answer.txt", content: new Uint8Array([104, 105]) }]);
      assert.equal(uploads[0].id, "F2");
      await context.replyRich({ text: "fallback", blocks: [{ type: "section", text: { type: "mrkdwn", text: "rich" } }] });
      await context.mirror({ ...context.message, id: "external-message", text: "outside input" });
      assert.equal((await context.conversation.history()).length, 3);
    } finally { await working.close(); }
    finished();
  });
  const running = app.run();
  void running.catch(failed);
  try {
    await fixture.send("<@B1> file", { files: [{ id: "F1", name: "hello.txt", size: 5, mimetype: "text/plain" }] });
    await complete;
    assert.equal(fixture.calls.find((call) => call.method === "F2").bytes.toString(), "hi");
    assert.equal(fixture.calls.filter((call) => call.method === "files.info").length, 1);
    assert.equal(fixture.calls.filter((call) => call.method === "assistant.threads.setStatus").at(-1).body.status, "");
  } finally {
    try { await app.close(); await running; } finally { await fixture.close(); }
  }
});

test("forced close cancels a detached native reply and never appends an uncertain send", { timeout: 10000 }, async () => {
  const fixture = await slackFixture({ replyDelayMs: 300 });
  const app = new AgentChat({
    channels: [new Slack({ botToken: "xoxb-fixture", appToken: "xapp-fixture", apiBaseUrl: fixture.apiBaseUrl, shutdownTimeoutMs: 50 })],
    onError: () => {},
  });
  let context;
  let replyOutcome;
  app.onMessage(async (current) => {
    context = current;
    const reply = current.reply("slow response");
    replyOutcome = reply.then(() => "sent", () => "cancelled");
    await reply;
  });
  const running = app.run();
  void running.catch(() => {});
  try {
    await fixture.send("<@B1> slow");
    await eventually(() => fixture.calls.some((call) => call.method === "chat.postMessage"));
    await app.close();
    await running;
    assert.equal(await replyOutcome, "cancelled");
    await new Promise((resolve) => setTimeout(resolve, 350));
    assert.equal((await context.conversation.history()).length, 1);
  } finally {
    try { await app.close(); } finally { await fixture.close(); }
  }
});

test("close stops intake and clears retained status guards concurrently before transport shutdown", { timeout: 10000 }, async () => {
  const fixture = await slackFixture({ statusClearDelayMs: 300 });
  const app = new AgentChat({ channels: [new Slack({ botToken: "xoxb-fixture", appToken: "xapp-fixture", apiBaseUrl: fixture.apiBaseUrl })] });
  const guards = [];
  let deliveries = 0;
  app.onMessage(async (context) => {
    deliveries += 1;
    guards.push(await context.working());
  });
  const running = app.run();
  void running.catch(() => {});
  try {
    for (let index = 1; index <= 4; index += 1) {
      await fixture.send("<@B1> work", { ts: `100.${index}`, thread: `100.${index}` });
    }
    await eventually(() => guards.length === 4);
    const start = Date.now();
    const closing = app.close();
    await eventually(() => fixture.calls.some((call) => call.method === "assistant.threads.setStatus" && call.body.status === ""));
    await fixture.send("<@B1> too late", { ts: "100.5", thread: "100.5" });
    await closing;
    await running;
    assert(Date.now() - start < 1000, "status cleanup should take one concurrent delay, not four serial delays");
    assert.equal(deliveries, 4);
    assert.equal(fixture.calls.filter((call) => call.method === "assistant.threads.setStatus" && call.body.status === "").length, 4);
    assert.equal(fixture.acknowledgements.some((ack) => ack.envelope_id === "env-100.5"), false);
  } finally {
    try { await app.close(); } finally { await fixture.close(); }
  }
});

test("close waits for an in-progress status creation and clears it before closing its transport", { timeout: 10000 }, async () => {
  let release;
  const statusStartGate = new Promise((resolve) => { release = resolve; });
  const fixture = await slackFixture({ statusStartGate });
  const app = new AgentChat({ channels: [new Slack({ botToken: "xoxb-fixture", appToken: "xapp-fixture", apiBaseUrl: fixture.apiBaseUrl })] });
  let creation;
  app.onMessage(async (context) => {
    creation = context.working().then(() => "created", () => "closed");
    await creation;
  });
  const running = app.run();
  void running.catch(() => {});
  try {
    await fixture.send("<@B1> status");
    await eventually(() => fixture.calls.some((call) => call.method === "assistant.threads.setStatus"));
    const closing = app.close();
    release();
    await closing;
    await running;
    assert.equal(await creation, "closed");
    assert.equal(fixture.calls.filter((call) => call.method === "assistant.threads.setStatus").at(-1).body.status, "");
  } finally {
    release();
    try { await app.close(); } finally { await fixture.close(); }
  }
});

test("a fatal transport error rejects run and aborts host work after native drain", { timeout: 10000 }, async () => {
  const fixture = await slackFixture();
  const app = new AgentChat({ channels: [new Slack({ botToken: "xoxb-fixture", appToken: "xapp-fixture", apiBaseUrl: fixture.apiBaseUrl, shutdownTimeoutMs: 30 })] });
  let context;
  let observedAbort = false;
  app.onMessage(async (current) => {
    context = current;
    await new Promise((resolve) => current.signal.addEventListener("abort", () => { observedAbort = true; resolve(); }, { once: true }));
    return "must not send";
  });
  const running = app.run();
  void running.catch(() => {});
  try {
    await fixture.send("<@B1> wait");
    await eventually(() => context !== undefined);
    const socket = await fixture.connection;
    socket.send(JSON.stringify({ type: "disconnect", reason: "link_disabled" }));
    await assert.rejects(running, /Socket Mode was disabled/);
    assert.equal(observedAbort, true);
    assert.equal(fixture.calls.some((call) => call.method === "chat.postMessage"), false);
  } finally {
    try { await app.close(); } finally { await fixture.close(); }
  }
});
