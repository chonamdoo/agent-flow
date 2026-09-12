---
name: webview-json-rpc-bridge
description: Implement or review WebView web-to-native JSON-RPC contracts, host transports, native capability authorization, and navigation lifecycle handling. Use when bridge messages or trust boundaries change; not native-module/JSI-only work or WebView inset-only layout.
---

# WebView JSON-RPC Bridge

Treat the bridge as an untrusted input boundary to native capabilities. JSON-RPC defines message meaning; the host transport defines delivery and sender evidence; the application defines authority and lifecycle. None of those contracts replaces the others.

## Establish the actual contract

Before changing a handler or client, identify:

- The host and installed versions: direct Android WebView, WebKit, React Native WebView, or another documented transport; inbound and outbound signatures may differ.
- The protocol version, method registry, parameter/result schemas, request ID policy, notification use, and whether batch support is part of the negotiated contract.
- The allowed content, navigation, frame, and origin policy, including initial loads, redirects, popups, local content, and embedded frames.
- How the host exposes trustworthy sender metadata, which native privileges each method requires, and what happens when content changes or the WebView is destroyed.
- Existing readiness, pending-request ownership, cancellation/timeout semantics, and compatibility fallback when the bridge or a capability is absent.

Trace one call from web encoding through native validation and authorization to response handling. This step is complete when the transport representation, protocol envelope, native authority, and pending-call lifecycle each have an identified owner.

Read [host-transports.md](references/host-transports.md) for the actual platform branch before asserting serialization or origin-verification behavior. If the required host capability or method policy is unknown, identify the missing contract and keep privileged dispatch unavailable; continue work that does not depend on that authority.

## Enforce protocol meaning

Use [JSON-RPC 2.0](https://www.jsonrpc.org/specification) when that is the adopted protocol. Preserve a documented legacy protocol unless migration is in scope; do not silently call a custom envelope fully JSON-RPC compliant.

### Requests and notifications

- A 2.0 request has `jsonrpc` exactly `"2.0"` and a string `method`. If `params` is present it is an object or array, then must satisfy the registered method's parameter schema. Names are case-sensitive; the `rpc.` prefix is reserved for protocol extensions.
- Detect `id` by member presence, not truthiness. The protocol permits string, number, or null IDs; null is discouraged and fractional numbers are discouraged. Prefer a collision-safe string ID policy for new calls, but preserve legitimate existing numeric IDs, including zero. An agreed narrower bridge profile must be explicit rather than misrepresented as the base specification.
- A notification omits `id`; `id: null` is not a 2.0 notification. A valid notification has no JSON-RPC response, including when its method fails. If a caller needs confirmation or a result, use a request instead.
- Malformed input without an ID is not automatically a notification. Distinguish an invalid request from a valid notification before applying the no-response rule.

### Responses

- A response requires `jsonrpc: "2.0"`, an `id`, and exactly one of `result` or `error`. A successful `result: null` is valid when the method's result schema allows it; a missing result is not null. Likewise, false, zero, and an empty string must not be rejected by a truthiness check.
- An error is an object with an integer `code`, string `message`, and optional `data`. Validate the envelope before correlating and validate a success result against the pending method's schema before resolving it.
- Match the ID value and type to a pending request in the current bridge lifetime. A response for numeric `1` must not settle string `"1"`. Reject malformed responses without treating them as successful completion or generating a response-to-response loop.
- Unknown, duplicate, and late responses must not settle unrelated work. A parse/invalid-request error may have `id: null` because no request ID was recoverable; it is not permission to fail an arbitrary pending request.

### Errors and batches

Keep parse failure (`-32700`), invalid request (`-32600`), unknown/unavailable method (`-32601`), invalid parameters (`-32602`), and internal error (`-32603`) distinct when implementing the protocol server. Use supported application error codes for application failures and preserve existing error semantics. Error details sent to the page must not expose native secrets, credentials, or stack internals.

If batches are supported, validate each member and correlate by ID, not array position. Responses may arrive in a different order. An empty batch is invalid; a batch of valid notifications produces no response, not an empty response array. If an existing bridge intentionally supports only single messages, document that restricted profile and its unsupported-input behavior; do not silently iterate arbitrary arrays or introduce batch dispatch during an unrelated fix.

## Keep native authority outside correlation

1. Establish the trusted content/navigation boundary using evidence the host actually supplies. A URL or origin field inside the JSON payload is attacker-controlled. A top-level trusted URL does not prove that a message came from a trusted frame.
2. Decode at the transport boundary and validate untrusted input before dispatch: protocol envelope, method registration, method parameter schema, and project-justified message size/depth and outstanding-work limits. Bound raw input before expensive parsing when the transport permits it, and bound the resulting structure before traversal/dispatch. Choose limits from supported workloads and threat constraints, not arbitrary universal numbers.
3. Dispatch only explicitly registered capabilities. Resolve authority from trusted native/session state and enforce the method's permissions, resource scope, and required user approval. Method names must not enable reflection, arbitrary handler lookup, code evaluation, or generic privileged command execution.
4. Recheck authority at execution where work crosses a navigation, permission, or session boundary. A validated envelope and a trusted page do not automatically authorize every native operation.

For transports without verifiable calling-frame origin, do not invent `event.origin` checks or accept a page-supplied origin as a substitute. Use an actually enforceable trusted-content configuration, a bridge-disabled view for untrusted content, or an explicit approved transport redesign. If those cannot establish the necessary trust, leave privileged capabilities unavailable and report the missing boundary. HTTPS protects transport; it does not by itself authorize a domain, frame, or method.

A request ID correlates a response. It is neither a secret capability nor a durable idempotency key. If the business operation requires duplicate suppression across retries or reconnects, use its adopted server/native idempotency contract independently of RPC correlation.

## Own the bridge lifetime

- Use the host's supported delivery shape. Serialize JSON text for a string transport and parse it at its receiver; retain supported structured objects on an object transport. Avoid double encoding or assuming every API named `postMessage` accepts the same type.
- Establish readiness before sending according to the existing protocol. A bridge global or matching user agent is not evidence that every native capability exists. Preserve documented feature detection or handshake behavior; do not add brand/version constants or a universal retry loop.
- Scope pending requests and delivery to the document/bridge lifetime. On reload, navigation to a new document, host destruction, or disconnection, settle or cancel owned pending work according to the existing contract, release listeners/timers, and prevent old replies from reaching a replacement document.
- Settle each pending request once. A timeout or transport loss means the caller lacks confirmation; it does not prove a native mutation never happened. Cancellation only means the native operation stopped when the method's contract and observed execution establish that fact.
- Preserve a legitimate browser fallback when the bridge is absent and the feature permits it. A privileged native action with no safe equivalent must report unavailability rather than fabricate success. Retries of side effects require the method's explicit safety/idempotency policy.

These lifecycle rules are author-derived engineering guidance, not additional JSON-RPC requirements. Apply them to the existing design without introducing an unrelated transport framework or app scaffold.

## Verify the changed boundary

Use an authorized local host and synthetic methods/data. Test the actual transport branch when runtime access is available; a JSON parser-only exercise cannot establish native frame isolation.

- A normal authorized request completes once with the intended native effect and matching result. A schema-permitted null result resolves successfully; missing `result`/`error`, both fields, or malformed error objects do not.
- A valid notification executes only if authorized and emits no protocol response. A malformed ID-less message follows invalid-request handling, not the notification shortcut.
- Unknown methods, invalid parameter shapes, excessive input under the adopted limits, and insufficient authority cause no privileged dispatch.
- An untrusted frame or navigated document cannot gain native authority, even with a valid envelope, known method, guessed request ID, or claimed trusted origin.
- Reload/disposal, late/duplicate/out-of-order replies, and overlapping calls do not cross-settle requests or leak native results into another document.
- If timeout, retry, batch support, or bridge-absent fallback changes, observe that branch and the method's actual effect; distinguish unconfirmed completion from proven failure.

Report changed protocol/transport boundaries, preserved compatibility exceptions, exercised host and version, observed effects, and unverified cases. Without a runnable host, provide source-grounded reasoning and identify the missing runtime proof; do not claim the bridge was secure on a device or that an operation ran.

## Sources and reuse notice

The authoring request supplied the boundary and authority criteria. Protocol rules come from [JSON-RPC 2.0](https://www.jsonrpc.org/specification), updated 2013-01-04. [Android native bridge security](https://developer.android.com/privacy-and-security/risks/insecure-webview-native-bridges), updated 2024-10-15, supports the frame/origin risk distinction. Platform API sources and version caveats are in the conditional host reference. Examples and verification scenarios here are author-derived, not recorded experiments.

The following notice is retained for the JSON-RPC explanatory material:

Copyright (C) 2007-2010 by the JSON-RPC Working Group

This document and translations of it may be used to implement JSON-RPC, it may be copied and furnished to others, and derivative works that comment on or otherwise explain it or assist in its implementation may be prepared, copied, published and distributed, in whole or in part, without restriction of any kind, provided that the above copyright notice and this paragraph are included on all such copies and derivative works. However, this document itself may not bemodified in any way.
