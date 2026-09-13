---
name: llm-tool-development
description: "Develop or review LLM function/tool schemas, dispatchers, execution authorization/approval, outcomes and replay, provider adapters, or MCP client/server tool implementations. Includes hosted-tool control integration; not ordinary tool invocation, connecting an existing MCP server, generic CLI tooling, or chat/prompt work without a tool execution boundary."
workflowPhases: [design, ddd-design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [LLM tool development, tool calling implementation, tool calling review, function calling implementation, function calling review, tool dispatcher, tool execution authorization, MCP server implementation, MCP adapter development, MCP server review, MCP adapter review]
requires_by_architecture:
  clean: [clean-architecture-core]
---

# LLM Tool Development

Apply the selected architecture contract (`clean-architecture-core` in Clean mode) and the implementation language's active guide. Local mode uses its full normative root and required references; execution authority, security, and effect-state safeguards below apply in every mode. This capability is language-neutral: it neither selects Python nor introduces an agent framework. Resolve provider API/SDK and MCP revision/capabilities from the integration being changed. Reuse existing application actions and their authorization, idempotency, egress, and audit policies rather than duplicating business logic for the LLM.

## Execution authority

Identify the actual executor for every tool: local application/client, provider-hosted built-in tool, or remote server. The responsibility flow is **model proposal → complete provider input → executor policy gate → existing application action → outcome reconciliation → matching call result**. Hosted execution may bypass the local dispatcher; locate the equivalent allowlist, authorization, approval, egress, and resource controls at the real executor. Disable a sensitive execution mode when equivalent required controls cannot be connected.

Tool registration, skill selection, annotations, model text, and approval prose grant no runtime authority. Enforce guards in executable policy at the actual boundary. Use trusted context for principal, tenant, credentials, allowed destinations, and effect classification. Model arguments are requests, not identity or permission evidence.

## Input and approval

- Define a narrow named action, its input/output meaning, read/write/external disclosure effects, limits, and expected failures. JSON Schema/strict mode constrains shape, not business validity, authorization, approval, or safe retry. Preserve absent/null and other wire meaning when adapting a provider's schema subset.
- Assemble and validate a **complete** call before dispatch; a parseable prefix of streamed JSON is not complete input. Validate size/depth/types/unknown fields and business invariants, then authorize action, object, tenant, and readable/writable fields. Scope lists/counts, bulk items, and delegated workers too. Schema-valid cross-tenant refunds must be denied.
- Bind human approval to tool/server identity, target, material arguments, and principal/tenant; use trusted records with scope/expiry appropriate to the action. Compare the approved meaning with the final normalized execution input and reapprove if it changes. Recheck authorization at execution, including revocation and target state. A generic earlier “yes” is not reusable write permission.
- Treat data disclosure as an effect: a read tool that sends private data to another service may require approval. Tool descriptions and MCP read-only/idempotency hints cannot downgrade this policy.

## Identity, concurrency, and result contract

Keep these identities separate, linked in host-owned execution records where needed:

| Identity | Meaning |
| --- | --- |
| Provider call ID | Correlates the provider's proposed call with its result. |
| Host execution ID | Identifies an execution attempt and its audit/recovery record. |
| Business idempotency key | Suppresses duplicate business effects within the declared tenant/operation scope and request fingerprint. |
| MCP request ID | Correlates a JSON-RPC request/response, not a durable business operation. |
| SSE event ID | Identifies stream delivery/resumption, not approval or effect uniqueness. |

A replayed provider event is not a new business request. Bound parallelism and run only independent authorized calls concurrently; sequential writes do not become an atomic transaction. Preserve dependency ordering and deterministic call/result correlation.

Use a typed outcome with public result/error semantics **separate from effect state**. Preserve expected validation/authorization/conflict failures without exposing secrets or raw infrastructure exceptions. Equivalent type names are acceptable if these distinctions survive:

| Effect state | Required evidence |
| --- | --- |
| `not_started` | No effect attempt began, such as policy rejection before dispatch. |
| `not_committed` | Attempted work is confirmed not applied. |
| `committed` | The defined effect is confirmed applied, even if response formatting failed. |
| `unknown` | Available evidence cannot determine whether the effect applied. |

Timeout, cancellation, connection loss, protocol errors, or provider error flags alone do not establish non-commit. A tool may report an error after a successful effect. Never blindly retry an unknown write: reconcile the operation/ledger/provider replay first, then retry only with established execution and backend idempotency guarantees. Durable claims bind tenant, operation, key, and request fingerprint; reject mismatched reuse and replay completed outcomes. Recover crashes after external success but before local result recording. Do not promise exactly-once execution or treat a call ID as its proof.

## Untrusted results and resource boundaries

- Preserve tool output, fetched documents, descriptions, and metadata as untrusted data in the provider's result envelope. Never promote embedded instructions into system policy. A malicious result does not authorize another call.
- Apply SSRF controls to schemes, hosts, ports, resolved addresses, redirects, and connection-time destinations; prevent DNS rebinding and access to disallowed internal/metadata services. Scope forwarded credentials to their intended audience.
- Constrain filesystem operations to authorized roots using safe path resolution and open operations; defend traversal, symlink escape, and check/use races. MCP roots are information, not an OS sandbox. Keep subprocess argument construction separate from shell interpretation.
- Use least-privilege credentials and permissions, secret/PII redaction, and bounded input/output bytes, nesting, calls, concurrency, queueing, duration, and retries. Audit tool/target/principal/approval/effect identity without logging raw secrets or unrestricted prompts/results.

## Conditional adapters

Load only the integration being changed:

- OpenAI function/custom tools, streaming, Responses result correlation, or hosted MCP: [OpenAI adapter](references/openai.md).
- Anthropic client/server tools, input streaming, or tool-result messages: [Anthropic adapter](references/anthropic.md).
- MCP server/client schema, authorization, transport, capabilities, or cancellation: [MCP adapter](references/mcp.md).

These references own version-sensitive envelope details, not shared business policy. API/model settings remain in the integration; no fixed model or effort is required.

## Evidence and review

Use only active authorized gates. For the changed boundary, observe allow/deny, approval binding, completed input, call/result linkage, and effect state. High-value counterexamples: schema-valid unauthorized target; arguments changed after approval; parseable partial stream; duplicate event; successful write with lost response; hostile result/URL/path; secret-bearing error. Name the actual executor exercised; provider schema acceptance or a local stub does not prove hosted execution controls.

Reuse State Integrity for durable outcome/replay, I/O Safety for egress/files/untrusted data, and Architecture Design for provider/action boundaries. Accept a pure read without a business idempotency ledger, or a provider-hosted call with equivalent enforced controls. Report observed evidence separately from static risk and unexecuted cases. Use existing phase markers, not new tool-specific required markers; server-only presentation stays `n/a`.

## Acceptance cases

These are expected outcomes for the affected scope, not claims of executed checks.

| Case | Observable acceptance |
| --- | --- |
| Authorized execution | One complete approved call reaches the intended action and its matching result records the confirmed effect state; denial produces no effect attempt. |
| Normal alternative | A bounded pure read needs no business idempotency ledger; provider-hosted execution is accepted when equivalent required controls are enforced at its real executor. |
| Core failure | Changed approved arguments, cross-tenant targets, and partial streams are denied; duplicate delivery does not duplicate a write; unknown outcomes are reconciled; hostile results/URLs/paths cannot expand authority. |
| Non-target | Ordinary use of an available tool, connecting an existing MCP server, or chat-only prompt work does not trigger tool-implementation requirements. |
| Conditional disclosure | Responses streaming reaches OpenAI; Anthropic result ordering reaches Anthropic; MCP transport/resumption reaches MCP. A provider-only change does not load all providers. |

## Official basis

- [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling) and [hosted MCP](https://developers.openai.com/api/docs/guides/tools-connectors-mcp).
- [Anthropic tool handling](https://platform.claude.com/docs/en/agents-and-tools/tool-use/handle-tool-calls).
- [MCP tools, revision 2025-11-25](https://modelcontextprotocol.io/specification/2025-11-25/server/tools) and [cancellation](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/cancellation).
