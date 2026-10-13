# Slack + Claude Agent SDK

Build `packages/node` first, then run `npm ci` here. Set `SLACK_BOT_TOKEN`,
`SLACK_APP_TOKEN`, and `ANTHROPIC_API_KEY`, then `npm start`. Node.js 22+ is
required by this example. Claude remains a normal JavaScript SDK dependency;
AgentChat does not embed it into Rust or require Python.

This is an ESM example and the AgentChat package currently exposes ESM imports
only. It accepts users who can reach the Slack bot; add your application's sender
and conversation authorization before the explicit `context.subscribe()` call.
Subscription allows untagged follow-up messages in that thread to resume the
same Claude session.

`createClaudeHandler` consumes `query()`'s async stream and returns only a
successful final result. It resumes a separate Claude session for each
`context.conversation.id`, closes each query, and forwards the application's
abort signal to the SDK. The map is in memory; persist it if sessions must
survive application restarts. The example disables tools explicitly. To enable
tools, select them and supply a permission policy appropriate to your application.

The Claude SDK manages a subprocess and ships platform components through npm
optional dependencies. Install optional dependencies, or configure the SDK's
`pathToClaudeCodeExecutable` when providing your own installation. Native
AgentChat likewise needs its matching platform binary. Browser or edge runtimes
without native addons and subprocess support are not targets for this example.

`npm test` uses a fake Claude stream and does not call Anthropic. The Node SDK's
tests exercise the actual Rust addon and a local Slack HTTP/WebSocket fixture.
`npm run typecheck` checks this example against the installed Claude SDK types.

References: [Claude SDK quickstart](https://code.claude.com/docs/en/agent-sdk/quickstart),
[session management](https://code.claude.com/docs/en/agent-sdk/sessions).
