# OpenAI Tool Adapter

Read only when implementing/reviewing OpenAI function/custom tool wire handling or hosted MCP controls. Resolve the selected API family, SDK version, tool type, and supported schema/stream behavior. This reference uses the official function-calling and Responses MCP contracts; it imposes no model, effort, or API migration.

## Application-executed calls

- Keep provider objects at the adapter. Responses function calls carry `call_id`; matching `function_call_output` uses that call ID, not an output-item ID, response ID, host execution ID, or business idempotency key. Chat Completions uses its own tool-call/result envelope; do not mix the two families.
- Iterate the actual typed output items rather than assuming the first output is a function call or final text. Preserve required conversation/output items for the selected API, including reasoning items when its contract requires them; that is an API continuation rule, not authority to execute a tool.
- Assemble streaming arguments by the call/item identity until the documented completion event. A `response.function_call_arguments.delta` fragment is incomplete input; validate the final arguments associated with `response.function_call_arguments.done` and the completed call before policy/approval/dispatch. Interrupted/truncated calls must not execute merely because accumulated text parses. Deduplicate delivery without confusing it with business idempotency.
- For strict function schemas, the documented subset requires `additionalProperties: false` and all declared properties in `required`; nullable types can encode optional values. Preserve the application's absent/null semantics explicitly rather than silently changing business input. Strict schema acceptance does not remove host validation or authorization. Custom free-form tools still require a constrained parser and policy.
- Return a matching result for each completed call with typed error/outcome semantics adapted to the wire envelope. Provider parallel-call options do not decide business independence or transaction scope. Bound actual execution separately.

## Provider-executed tools and hosted MCP

OpenAI may call the remote server itself; a local dispatcher is not necessarily in that execution path. Resolve the trusted server/connector identity and restrict allowed tools. Route `mcp_approval_request` to the authorized approval boundary and bind the corresponding `mcp_approval_response` to the exact approved tool/target/arguments/principal. Preserve the approval request ID separately from the business idempotency key.

Do not copy a quickstart's `require_approval: never` into a sensitive workflow. Approval and API allowlists do not replace the remote server's tenant/object/field authorization or credential audience restrictions. If the hosting mode cannot enforce the required controls, do not enable that sensitive mode. Remote output and tool metadata remain untrusted data; secrets sent to the server are real disclosures.

## Observable cases

A duplicate completed function-call event returns/reconciles the prior execution rather than charging again. A partial argument stream is not dispatched. A hosted call is accepted only when the provider/server controls enforce the same required policy, not because a local fake dispatcher denied a similar call. Unknown hosted write outcomes remain unknown until reconciled. Ordinary use of an existing OpenAI tool is outside this development reference.

## Official sources

- [Function calling](https://developers.openai.com/api/docs/guides/function-calling): strict schema, streaming, call/result correlation.
- [Structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs): supported schema limitations.
- [MCP and connectors](https://developers.openai.com/api/docs/guides/tools-connectors-mcp): execution authority, approvals, filtering, and disclosure risks.
