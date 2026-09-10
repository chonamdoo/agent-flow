# Anthropic Tool Adapter

Read only for Anthropic client/server tool schemas, streaming input, or result messages. Resolve SDK/API version, selected tool type, and supported features; no model, effort, or beta default is prescribed.

## Client-executed tools

The adapter receives `tool_use` blocks with `id`, `name`, and `input`; host policy authorizes the action before execution. Return a `tool_result` block using the matching `tool_use_id` in the following user message, with result blocks before ordinary text. Preserve the API's required adjacency and handle every pending call, rather than converting results into system instructions or unrelated user text. Toolset-specific fields and allowed content types must follow the selected toolset's versioned contract.

For streamed/fine-grained input, assemble the complete tool-use input through the documented content-block completion. Partial JSON may be invalid or temporarily parseable; neither is an execution boundary. Handle truncation/interruption without attempting a write. `strict` applies only to its supported schema and API features, not authorization, tenant policy, or approval.

`is_error` communicates tool execution error, not effect rollback. An error after a committed write still requires committed/unknown effect handling. Generic SDK Tool Runner retries must obey the host's effect/idempotency policy; an automatic conversation loop cannot authorize another write.

## Server-executed tools

Server tools execute provider-side and have different result handling. A response can contain client calls and unresolved server calls: continue according to the selected API's stop-reason/result rules instead of executing the server tool locally or fabricating completion. In the documented mixed-call path, return only client tool_result blocks while pending server work continues; consult the current contract before modifying tools or appending unrelated text.

Locate actual provider-side limits, approvals, credential scope, and downstream authorization. If required sensitive-action controls cannot be enforced, the execution mode is not eligible. A model's selection or a server-tool label is not permission. Keep provider result metadata out of application business types.

## Observable cases

A client result must correlate to its own call even when several calls finish out of order. Changed refund arguments invalidate earlier approval. A timeout after an external success cannot become `not_committed` because `is_error=true`. A provider-hosted read with properly enforced disclosure controls is valid; requiring every tool to run locally would be an overconstraint. General Claude chat without an execution boundary does not activate this reference.

## Official sources

- [Handle tool calls](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls): client/server ownership, result ordering, error and untrusted-content semantics.
- [Strict tool use](https://platform.claude.com/docs/en/agents-and-tools/tool-use/strict-tool-use).
- [Fine-grained tool streaming](https://platform.claude.com/docs/en/agents-and-tools/tool-use/fine-grained-tool-streaming).
- [Handling stop reasons](https://platform.claude.com/docs/en/build-with-claude/handling-stop-reasons).
