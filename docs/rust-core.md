# Rust core and language interfaces

AgentChat uses a shared Rust turn engine with Python, Node.js/TypeScript and Rust
interfaces. The application still owns its agent framework, sender authorization,
model configuration and agent session storage.

## Python compatibility

Existing imports, dataclasses, channel objects, decorators and async context
methods keep working:

```python
from agentchat import AgentChat
from agentchat.channels.slack import Slack

app = AgentChat(channels=[Slack.from_env()])

@app.on_message
async def respond(context):
    return context.message.text
```

Rust owns turn transitions and the default in-memory claims/history store. The
Python adapter executes effects on the original asyncio task, including the
entire scope of a custom `State.lock()` context manager. It does not serialize
Python messages or arbitrary metadata through JSON. Dataclass identity, custom
state, custom channels, exceptions, cancellation and application overrides of
`reply()` are preserved. The native store participates in Python garbage
collection, including cycles through message metadata.

Select `AgentChat(..., backend="python")` to use the previous dispatcher and
memory implementation. `AGENTCHAT_BACKEND=python` changes the default for testing
or rollback. An explicit constructor option takes precedence. Backend selection
is immutable for an application instance and failures never replay through the
other backend. Existing channel objects keep their current Python SDK transport;
selecting the Rust core does not replace a constructed channel.

Installing from a wheel needs no Rust toolchain. Installing this checkout or a
source distribution builds `agentchat._native` with maturin and requires Rust.
The Python minimum remains 3.11. Ordinary CPython wheels use `abi3-py311`;
free-threaded Python and alternative interpreters are not qualified by this
release. The Python fallback is a runtime rollback option, not a replacement for
a missing native binary during installation.

## Node and Claude Agent SDK

Build the package from this checkout:

```bash
cd packages/node
npm ci
npm run build
npm test
npm run smoke
```

See the [Node package](../packages/node/README.md) and
[Claude example](../examples/node/README.md) for the JavaScript API and a Slack bot
using `@anthropic-ai/claude-agent-sdk`. The Node package is a native napi-rs addon;
it needs no Python installation. Node 22 and 24 are the initial CI targets.
Browser/edge runtimes, Bun and Deno are outside this initial support matrix.

Claude executes in the Node handler. Map each AgentChat conversation ID to a
Claude session ID, resume it for subsequent turns, and return the successful
final result for automatic delivery. AgentChat history and Claude sessions are
separate stores. An uncertain Slack reply does not warrant rerunning Claude or
replaying a send. Forward cancellation into the Claude SDK and close AgentChat
explicitly during application shutdown.

## Rust interface

`crates/agentchat` is the public Rust facade. Enable its `slack` feature for the
native provider. `crates/agentchat-core` owns typed models, state contracts,
conversation ordering, context operations and the turn engine. Custom channels
implement the open `Channel` trait; they do not need registration in a closed
provider enum. Rust applications use their own Tokio runtime.

Provider crates own normalization, route validation, authentication, transports
and confirmed receipts. Language bindings do not parse Slack routing fields or
implement independent turn algorithms. Shared domain results use named structs
and enums. IDs stay strings across the public interfaces to avoid JavaScript
integer precision loss.

## Current channel support

| Channel | Python interface | Node and Rust interfaces |
| --- | --- | --- |
| Slack | Existing compatible Python adapter, shared Rust core | Native Rust adapter |
| Telegram | Existing compatible Python adapter, shared Rust core | Native port pending |
| WhatsApp | Existing compatible Python adapter, shared Rust core | Native port pending |
| Discord | Existing compatible Python adapter, shared Rust core | Native port pending |
| iMessage | Existing compatible Python adapter, shared Rust core | Native port pending |
| Teams | Existing compatible Python adapter, shared Rust core | Native port pending |

This is the first migration stage: the shared core and Slack path are available
without rewriting existing Python integrations. Remaining native providers can
be added behind the same channel contract. Their Python transports remain
available throughout migration.

Claims/history are in-process and unbounded, matching the previous default.
Claims survive handler failures within that state and disappear on restart.
There is no durable queue or exactly-once guarantee. Native Slack makes a single
write attempt and only records confirmed receipts; rate-limit scheduling and
uncertain-delivery recovery remain application concerns. Existing Python
provider-specific behavior is retained.

## Checks and binary qualification

Run these checks before proposing a change:

```bash
python -m pip install -e '.[dev]'
ruff check .
python -m pytest
AGENTCHAT_BACKEND=python python -m pytest
cargo fmt --all --check
cargo test --workspace --locked
cargo clippy --workspace --all-targets --locked -- -D warnings
```

Node and Claude example commands are in their respective READMEs. Tests use
injected/local Slack HTTP and WebSocket servers and a fake Claude stream, so they
do not send messages to a workspace or require model credentials. A live echo
check with the application's real Slack installation and Claude credentials is
still needed before deploying that application.

CI exercises both Python backends, the Rust contracts, Node/Claude flows and
installed packages. Binary jobs build and smoke-test Linux glibc x64/arm64,
macOS x64/arm64 and Windows x64. These are qualification targets: support is
confirmed by passing jobs, not merely by successful cross-compilation. Source
distribution installation is checked independently. Musl and other targets
require separate qualification.

Coordinate future PyPI, npm and Cargo releases from the same source revision and
version. Build and test the complete binary set before publishing; no workflow
in this change publishes to a registry automatically. Include the core/package
version and platform when reporting a binding issue. Changes to shared behavior
must pass all language contract tests; new provider support must update this
matrix and include setup documentation and an example.
