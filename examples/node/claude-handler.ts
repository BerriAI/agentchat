import type { Options, SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import type { MessageHandler } from "@berriai/agentchat";

export type ClaudeQuery = (input: { prompt: string; options: Options }) => AsyncIterable<SDKMessage> & { close?: () => void };

/** One SDK session per chat conversation. AgentChat serializes turns in that conversation. */
export function createClaudeHandler(
  query: ClaudeQuery,
  options: Omit<Options, "resume" | "abortController"> = {},
): MessageHandler {
  const sessions = new Map<string, string>();
  return async (context) => {
    const abortController = new AbortController();
    const abort = () => abortController.abort();
    if (context.signal.aborted) abort();
    context.signal.addEventListener("abort", abort, { once: true });
    let stream: ReturnType<ClaudeQuery> | undefined;
    try {
      context.signal.throwIfAborted();
      stream = query({
        prompt: context.message.text,
        options: {
          ...options,
          resume: sessions.get(context.conversation.id),
          abortController,
        },
      });
      let result: string | undefined;
      let successfulSession: string | undefined;
      for await (const event of stream) {
        context.signal.throwIfAborted();
        if (event.type !== "result") continue;
        if (event.subtype !== "success" || event.is_error || !event.result) {
          throw new Error(`Claude did not return a successful answer (${event.subtype})`);
        }
        result = event.result;
        successfulSession = event.session_id;
      }
      context.signal.throwIfAborted();
      if (result === undefined || successfulSession === undefined) {
        throw new Error("Claude ended without a successful result");
      }
      sessions.set(context.conversation.id, successfulSession);
      return result;
    } finally {
      context.signal.removeEventListener("abort", abort);
      stream?.close?.();
    }
  };
}
