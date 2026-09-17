# Mock Execution and Contract Evidence

Use when a Next.js API-routing task changes network mocks, switches between mock and real upstreams, or relies on mock results as evidence. Keep the project's existing HTTP mock, MSW setup, or other established tool when it meets the need. This reference does not require MSW, Pact, a new contract framework, or a fixture-directory migration.

## Locate the Interception Boundary

Record both the caller's process and the point where the response is substituted. Tool choice and evidence strength are separate questions.

| Mechanism | What it can affect | What a passing result does not establish |
| --- | --- | --- |
| Separate HTTP mock service | Requests actually addressed to that service from browser, Next server, build, or another consumer | A skipped proxy/rewrite, real provider behavior, or a deployed host path that was not exercised |
| MSW browser worker | Browser requests within the active worker's supported scope | RSC/SSR/Route Handler outgoing requests in the Next process; a rewrite bypassed by a synthetic browser response |
| MSW Node `setupServer` | Supported outgoing request APIs in the Node process where interception is installed | A separately launched Next server, another worker/process, or an Edge runtime merely because the test runner uses MSW |
| Injected data, props, or function stub | The consumer behavior reached after that seam | URL resolution, HTTP encoding, status/header handling, or any network hop bypassed by the injection |

A browser can request an RSC payload, but intercepting that browser request is not intercepting the backend fetch performed while rendering the RSC. A test runner's `setupServer()` does not create a listening HTTP mock service. If Next runs separately, either use an already approved integration inside that process with compatible APIs or point the actual Next upstream to a reachable HTTP mock. Preserve build-time versus request-time execution and the configuration binding rules in the parent skill.

For example, browser → Next rewrite → HTTP mock can exercise that rewrite if both Next and the mock observe the request. Browser → HTTP mock directly cannot. Browser MSW responding to `/api/items` may satisfy the UI before Next sees anything. Record which path ran rather than ranking one tool as universally stronger.

## Activate Only the Intended Mock Environment

1. Discover the installed mock-library version, runner/browser integration, request client, enabled environments, and fixture source. Current MSW examples use `http`/`HttpResponse` and `msw/browser`/`msw/node`; older releases differ. Check exports and matching documentation before adapting an existing implementation. A network-mocking task does not authorize an upgrade.
2. For browser MSW, serve the matching worker asset at the configured URL and scope, then await `worker.start()` before requests that depend on it. Confirm actual interception, not only a “mocking enabled” console message. A worker registration race can let initial requests reach the real network.
3. For Node MSW, enable interception before outgoing requests in the target process. The documented `server.listen()` is synchronous; use the runner's existing setup/teardown hooks rather than copying browser startup semantics. Check support for the installed request client/runtime instead of assuming every transport is intercepted.
4. Keep mocking explicitly limited to the intended development, test, or preview environment. When the changed API must be isolated, make an unmatched request observable or fail through the existing harness; explicitly allow unrelated assets/services where needed. Do not convert every real API failure into mock success or let unexpected passthrough make production writes. Use synthetic data and a safe upstream.

Setup is complete when a representative request is observed at the intended interception point and the team can distinguish mock response, deliberate passthrough, and unexpected network escape. Installation commands in library documentation are not permission to run them.

## Model the Contract, Not Just a Happy Payload

Use the existing API specification and observed/provider-confirmed behavior as the fixture source. Identify who maintains it when multiple mock consumers share handlers; avoid independently drifting browser and HTTP mock contracts. Do not add a second schema merely to make a fixture validate itself.

For the changed operation, represent the relevant request and response behavior:

- Match the actual method, origin/path, query encoding, request media type, and meaningful body fields. An overly broad wildcard can conceal a wrong URL or method.
- Return realistic status, headers, and body together. Select only cases the contract supports: empty versus populated results, validation rejection, unauthenticated versus forbidden, conflict, rate limiting, unavailable upstream, or transport failure. An HTTP error response is not the same as a failed network request.
- Preserve null/absent meanings, pagination envelopes, identifiers, error field paths, multipart/binary responses, and bodyless success when those are part of the operation. A handler that accepts any payload and always returns success cannot show request-contract correctness.
- Use controlled latency or an out-of-order response only where the changed caller's behavior depends on it. Exercise the actual caller's pending/error/result handling; leave Query, form, and store lifecycle policy with their existing owners.

For Storybook fixtures or `play` evidence, read `react-storybook` only when that surface is being changed. Error props demonstrate presentation; actual network and form behavior require the corresponding pipeline to run. If submission semantics change, use `react-tanstack-form` or `react-hook-form-zod` according to the installed form in scope, not both by default.

## Reset Handlers and Application State Separately

`server.resetHandlers()` or `worker.resetHandlers()` without arguments removes runtime handlers added through `use()` and returns to the initial handler list. Passing handlers replaces that list. It is a handler-list operation, not a reset of everything reachable from a handler.

Name the reset owner for each mutable resource actually present:

| Resource | Separate isolation responsibility |
| --- | --- |
| Handler overrides and mock lifecycle | Reset the intended list between runs; stop/close interception through the existing integration when its lifetime ends |
| Mock database, counters, handler closures, external HTTP mock state | Recreate, reseed, or explicitly reset the underlying data, not merely the handler list |
| In-flight work, timers, subscriptions | Settle, cancel, or scope work so a late result cannot modify the next run |
| Query/cache, persisted client state, stores, form instances | Use their existing lifetime/reset APIs or fresh instances; handler reset does not evict cached results or discard drafts |
| Browser cookies and storage, server sessions, Next/host caches | Isolate or reset only the authorized synthetic scope; a cookie reset is not provider session revocation or a server-cache reset |

For parallel scenarios sharing an HTTP mock, use supported per-run isolation or independent instances instead of racing a global reset. For sequential scenarios, change or fail one scenario and then revisit another: it must start from its declared baseline. If a request never leaves a client/server cache, its success is not evidence that the newly selected handler ran. These checks identify state owners; they do not impose a universal app-wide reset strategy.

## Keep Evidence Claims Separate

- **Consumer behavior:** a fixture or network mock can show how the actual consumer handles the supplied response. Name any code bypassed by injection.
- **Wire shape:** OpenAPI/schema checks can detect mismatched fields, formats, methods, or status schemas to the extent the checked specification describes them. Agreement between a mock and a self-authored schema does not prove either matches the provider.
- **Provider compatibility:** provider-verified consumer contracts or authorized real-service requests can establish the behavior they exercise against a specific provider version/environment. Pact is one possible tool, not an adoption requirement.
- **Business, authentication, and deployment:** persistence, authorization, browser cookie acceptance, concurrency invariants, and host routing need their respective real boundaries to run. Neither schema validation nor a passing provider contract automatically establishes all of them. Use `nextjs-auth-session` conditionally when actual auth/session evidence is at issue.

Select the smallest authorized scenario that answers the changed contract question. For a mock-to-provider cutover, compare the same representative request/result semantics through the intended routing path; use safe synthetic operations and credentials. Record caller process, environment/provider version when available, traversed hops, interception point, observed request/result, and the remaining unexercised boundaries. Redact secrets and personal data.

A useful conclusion says “the browser consumer handled the mocked 409 response” or “Next forwarded this multipart request to the HTTP mock.” It does not promote either result to real provider correctness. If provider access is unavailable, deliver the consumer/routing findings and leave provider behavior explicitly unverified.

## Sources and Version Limits

These are official rolling MSW documentation links, not an installed-version guarantee. The process-boundary conclusions follow from the documented browser Service Worker and current-process Node mechanisms. Use matching version documentation and installed exports for exact setup, reset, and transport support.

- [Browser integration](https://mswjs.io/docs/integrations/browser/): worker asset, activation, and awaiting startup before dependent requests.
- [Node integration](https://mswjs.io/docs/integrations/node/): current-process interception, synchronous `listen`, and runner lifecycle.
- [Server resetHandlers](https://mswjs.io/docs/api/setup-server/reset-handlers/) and [worker resetHandlers](https://mswjs.io/docs/api/setup-worker/reset-handlers/): handler-list reset/replacement semantics.
- [Next BFF guidance](https://nextjs.org/docs/app/guides/backend-for-frontend#server-components): server caller and build/request-time execution boundaries.

The scenario selection and evidence distinctions are author-derived review guidance. They are not claims that a particular app, provider, mock integration, or skill invocation has been tested.
