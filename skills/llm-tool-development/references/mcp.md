# MCP Tool Adapter

Read only for MCP client/server implementation or review of schema, authorization, transport, capability negotiation, or cancellation. The cited baseline is revision **2025-11-25**; negotiate and apply the peer's actual supported revision and SDK. Do not silently enable capabilities or treat this revision as a mandatory upgrade.

## Protocol boundary

- Preserve initialize/version/capability negotiation and use only negotiated facilities. Declaring `tools` enables the tool protocol; `listChanged` governs tool-list change notifications. Handle tools/list pagination and changes without silently treating an unreviewed replacement tool as the previously approved tool.
- Tool names are unique within a server, not across all servers. Resolve server plus tool identity in policy and approval. Schema conversion must retain semantic validation, missing/null meaning, and declared output structure.
- In the cited revision, inputSchema is a valid JSON Schema object, not null; outputSchema is optional. Validate structuredContent against a declared outputSchema and retain the supported content envelope. Resource links/content are data, not permission to fetch arbitrary URIs or execute instructions.
- Distinguish JSON-RPC protocol failures from a tool result with `isError`. Neither flag proves the business effect was not applied. Preserve the host's typed result and effect state through mapping.
- JSON-RPC request ID correlates messages. SSE event ID supports delivery/resumption. Neither is an idempotency key, an approval binding, or a transaction identity. Resumption/redelivery must not create a new business effect.

## Authorization and transport

Validate tokens for the intended resource/audience and required scopes at the actual server. Do not pass through an incoming token to an unrelated downstream service; use the appropriate trusted credential/delegation flow. Client-side policy and human approval supplement server-side tenant/object/field authorization, not replace it.

For HTTP transports apply the revision's Origin validation, authentication/session handling, and secure binding rules to defend DNS rebinding and cross-origin abuse. A session ID is not authentication. Check redirects and resolved destinations at each outbound boundary, including resource/icon URLs. For local stdio processes, use least-privilege execution, scoped environment/credentials and safe arguments; local transport is not inherently trusted.

Tool annotations such as readOnlyHint/destructiveHint/idempotentHint are hints. They cannot grant permissions or demonstrate safe replay. MCP roots communicate intended roots; enforce actual filesystem bounds against traversal/symlinks/check-use races with OS-level controls where needed.

## Cancellation and long execution

Cancellation is a request with races: the peer may have completed already or be unable to stop. Preserve unknown effect state until reconciled. A cancelled HTTP/SSE connection is not a rollback. If the chosen revision/peers support task-augmented execution, negotiate it and distinguish task lifecycle/status from business commit state; it is not a universal MCP facility or durable-effect guarantee.

## Observable cases

A schema-valid cross-tenant call is denied at the server. A resumed SSE event maps to the existing execution. A malicious tool-description update does not inherit approval. An allowed root containing an escaping symlink is still denied by filesystem enforcement. A trusted, bounded read without a business idempotency ledger is valid. Merely connecting to or using an existing MCP server is not this skill's implementation scope.

## Official sources

- [Tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) and [lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle).
- [Authorization](https://modelcontextprotocol.io/specification/2025-11-25/basic/authorization) and [transports](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).
- [Cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation), [roots](https://modelcontextprotocol.io/specification/2025-11-25/client/roots), and [tasks](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks).
