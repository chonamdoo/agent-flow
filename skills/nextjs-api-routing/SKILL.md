---
name: nextjs-api-routing
description: Implement or review Next.js browser/RSC upstream routing, rewrites versus direct API or BFF, environment binding, proxy response contracts, and network-mock coverage. Not generic Next.js UI, standalone auth policy, Query cache policy, or non-Next mocking adoption.
workflowPhases: [design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [Next.js rewrites, Next API proxy, Next.js BFF, Next upstream routing, Next API mocking]
---

# Next.js API Routing

Trace the request that actually runs before changing its route. Preserve working direct APIs, simple rewrites, server-only fetching, and existing HTTP mocks. This skill does not authorize package installation, architecture migration, deployment, or production requests.

## Establish the Request Path

1. Discover the installed Next.js version, App/Pages/mixed router, Node or Edge runtime, deployment adapter, and output mode from manifests, lockfiles, configuration, and deployment scripts. Current documentation is not proof that an API exists in the installed release. Use the installed Proxy/Middleware convention; a routing task alone does not require renaming it.
2. Identify each changed caller and execution time: browser interaction, Server Component prerender, request-time RSC/SSR, Route Handler/API route, Server Action, or test process. Record its origin/base URL, intended upstream, and any CDN, host proxy, Next rewrite, or BFF it traverses. A browser call to `/api/...` and an RSC call are different paths even if they read the same resource.
3. Locate where the upstream value is read and bound: build, process startup, request, or separately managed host configuration. Inspect existing build artifacts and host configuration when available; distinguish intended environment settings from the destination actually deployed. Record secret presence without exposing values.
4. Identify the existing API contract and policy owners: methods, request/response media types, meaningful status codes, required headers, credential destination, and any deliberate transformation. Resolve an unknown provider contract before inventing forwarding or authentication behavior.

Discovery is complete when every changed caller has a concrete process, destination chain, configuration binding time, and contract owner. Missing deployment access limits deployment conclusions; it does not prevent source-grounded routing review.

For mock setup, mock-to-real-server switching, or claims based on network fixtures, read [Mock execution and contract evidence](references/mock-contracts.md). It covers browser/Node/HTTP scope and state isolation; it is not needed for a routing-only change without mocks.

## Choose the Smallest Routing Boundary

| Existing or proposed path | Appropriate use | Constraint to retain |
| --- | --- | --- |
| Browser directly to API | The independent API intentionally supports browser access | CORS, preflight, credential mode, and cookie scope must work for the actual origins/sites. Server secrets cannot move into browser code. |
| Browser through Next `rewrites` | Mapping a public path to an upstream without application-level transformation | A URL proxy is not a new authentication or response-mapping policy. Keep simple rewrites when they satisfy the contract. |
| Browser through BFF | A demonstrated need for server-held credentials/token exchange, API composition, or explicit request/response adaptation | Use the existing Route Handler or Pages API route boundary. It is publicly callable and adds an HTTP hop and response owner. Mere use of Next.js or cookies is not a reason to add one. |
| RSC/SSR directly to backend or existing server data-access seam | Data is needed on the server | Avoid a call through the app's own public Route Handler just to reuse code. Share the appropriate server function instead. Preserve existing authorization and deliberate remote service boundaries. Server-only fetching does not require TanStack Query. |
| Static export with direct API or external CDN/reverse proxy | Static assets plus browser data access or host-managed routing meet the product need | Next runtime rewrites, dynamic BFF endpoints, and Server Actions are unavailable in the export. External host routing is a separate valid capability, not a Next rewrite. |

Static export can still prerender Server Components and supported static GET outputs at build time. Do not confuse those generated files with request-time server execution. RSC fetching also does not inherit the browser's cookie jar: use only the established credential forwarding boundary for the intended backend, and do not assume an upstream `Set-Cookie` during rendering reaches the browser.

When a routing change touches session verification, protected operations, credential/cookie mutation, CSRF, or authenticated cache isolation, read `nextjs-auth-session` for that policy. Do not replace it with cookie-presence checks or a new BFF policy. If actual Query transport/cache behavior changes, use `react-tanstack-query` conditionally; an upstream route change alone is not a reason to install Query.

## Bind Configuration at the Right Time

- Direct `NEXT_PUBLIC_*` references bundled by Next are build-frozen. Changing the deployed process environment does not rewrite existing browser assets. A deliberately implemented public runtime-config endpoint or host-injected public configuration is a separate mechanism; it must not expose secrets.
- Values placed in `next.config.js`'s `env` option are build substitutions included in JavaScript regardless of the variable's name. A missing `NEXT_PUBLIC_` prefix does not make that option secret-safe. Also inspect serialized props, responses, errors, and logs for accidental disclosure.
- An env-derived `next.config` rewrite destination is not a per-request env lookup. Standard production builds materialize rewrites in the routes manifest; the pinned Next source below demonstrates this mechanism. Inspect the installed version's output and deployment adapter before asserting when a promoted image picks up a new upstream. Restarting with a different env is not evidence that the compiled destination changed.
- A private server env value can be read at request time in a supported dynamic execution path, but it can also be captured at module initialization, config evaluation, prerender, or cache creation. Classify the actual read and result lifetime rather than labeling every private variable “runtime.” Use the installed release's supported dynamic APIs only where request-time behavior is required.

Configuration work is complete when the proposed environment change has an identified effect on the built artifact, restarted process, request-time code, or host rule. Keep independently deployed host routing separate from Next configuration.

## Resolve Matching and Precedence

Inspect the entire overlapping rule set, not only the new pattern. Current Next routing checks headers, redirects, Proxy, `beforeFiles`, filesystem routes, `afterFiles`, dynamic routes, then `fallback`; an array of rewrites runs after filesystem checks and before dynamic routes. `beforeFiles` continues through its rules rather than immediately resolving the filesystem after each match. Check the installed version and Pages fallback behavior when relevant.

Account for wildcard versus single-segment matching, parameter/query forwarding, `has`/`missing`, `basePath`, applicable locale handling, trailing slashes, and competing Route Handlers/API routes. These features are version- and router-sensitive. Include a matching request, a nearby non-match, and any real overlap in the decision evidence. A catch-all must not accidentally capture assets, authentication callbacks, or a local API route. Trace host/CDN precedence too: a correct Next rule cannot govern a request the host sends elsewhere.

## Preserve the Wire Contract

Apply these checks to the existing proxy or BFF; they are not a requirement to write a generic forwarding framework.

- **Destination and method:** derive the upstream from trusted configuration and constrained route inputs. Untrusted URLs or redirect targets must not turn the endpoint into an open proxy/SSRF path. Preserve supported methods, query parameters, and the contract's redirect behavior; an automatically followed upstream redirect can hide its original status or change the resulting request.
- **Body:** preserve the required JSON, multipart, binary, or streaming representation. Request bodies are consumable streams; parsing and then forwarding the already-consumed body is not transparent forwarding. Preserve multipart boundaries, avoid JSON conversion for arbitrary payloads, and keep bodyless responses such as `204` and `HEAD` bodyless. Verify runtime/host support when streaming or upload limits matter.
- **Status and response:** retain upstream status/error meaning unless an explicit BFF contract maps it. A non-2xx upstream response must not silently become `200`; an empty response must not require JSON parsing. Treat transport failure separately from a provider rejection. Preserve relevant media type, cache, redirect, and content-disposition semantics; recalculate or remove stale representation headers when transforming the body.
- **Headers:** forward a contract-specific allowlist to the intended upstream, not every incoming header. Decide credential headers explicitly; do not copy `Host`, hop-by-hop transport headers, or caller-supplied trusted-proxy identity indiscriminately. Separate upstream request headers from browser response headers. Copying authorization/cookies/internal headers into a response leaks them; removing all headers can break the contract just as surely.
- **Cookies:** preserve each required `Set-Cookie` as a separate value. Never comma-join cookies or split a combined value naively on commas (`Expires` contains commas). On a supporting server runtime, `Headers.getSetCookie()` supplies the individual values; otherwise use the installed stack's supported multi-value API. Browser JavaScript cannot read network `Set-Cookie` to verify this.
- **Cookie scope:** evaluate Domain/Path against the browser-facing URL, not only the backend URL. A backend-domain cookie can be rejected behind another public host; a backend path may not match the public path. Any translation must follow the established cookie contract, including attributes and deletion scope, rather than blindly stripping Domain or broadening Path. Confirm all intended cookies in the browser and on the next request without logging their values.

## Completion Evidence

For design/review, present the chosen path, preserved alternative, exact code/config evidence, and any missing deployment/provider fact that affects the conclusion. A diagram or configuration snippet alone does not establish runtime success.

For implementation, exercise the changed caller through the project's authorized runtime and declared checks. Record the actual outgoing destination and traversed hops, method, payload/media type, relevant status/header behavior, and caller-visible result. Cover the meaningful overlap or failure branch introduced by the change, not an arbitrary list of scenarios. For cookies, observe the outgoing multi-value headers, browser acceptance, and next request. For configuration promotion, exercise the built artifact with the intended environment/host rule; `next dev` success is not deployed-path proof.

Report source reasoning, executed evidence, and unrun scenarios separately. A request made directly to an HTTP mock cannot prove a skipped rewrite; a mocked upstream cannot prove provider authorization or persistence. When execution is unavailable, state the exact missing runtime or access rather than asserting verified deployment behavior.

## Sources and Applicability

These routing decisions and completion criteria are author-derived applications of the official references, not reports of app tests. Consult matching installed-version documentation/types and actual host capabilities before copying APIs. The rolling Next pages read for this skill identify version **16.3.5**; that is source context, not a minimum version or compatibility guarantee. The pinned **v16.0.0** implementation below supports only the stated build-manifest mechanism. Source skill availability does not establish installation or automatic selection in a target project.

- [Next rewrites](https://nextjs.org/docs/app/api-reference/config/next-config-js/rewrites): matching, ordering, external destinations, and parameter handling.
- [Next BFF guidance](https://nextjs.org/docs/app/guides/backend-for-frontend): public endpoints, body handling, direct Server Component data access, and deployment constraints.
- [Environment variables](https://nextjs.org/docs/app/guides/environment-variables) and [next.config env](https://nextjs.org/docs/app/api-reference/config/next-config-js/env): public inlining, explicit bundle substitution, and dynamic server reads.
- [Next v16.0.0 build source](https://github.com/vercel/next.js/blob/v16.0.0/packages/next/src/build/index.ts) and [production route-manifest loading](https://github.com/vercel/next.js/blob/v16.0.0/packages/next/src/server/lib/router-utils/filesystem.ts): materialized rewrite rules rather than per-request config evaluation.
- [Static exports](https://nextjs.org/docs/app/guides/static-exports) and [self-hosting](https://nextjs.org/docs/app/guides/self-hosting): build outputs, external hosting, runtime environment, and proxy/streaming limits.
- [NextResponse](https://nextjs.org/docs/app/api-reference/functions/next-response): upstream versus response headers and defensive forwarding.
- [RFC 6265 sections 3 and 4.1.2](https://httpwg.org/specs/rfc6265.html): separate cookie fields and Domain/Path semantics. [Headers.getSetCookie](https://developer.mozilla.org/en-US/docs/Web/API/Headers/getSetCookie): server API and browser filtering; check runtime availability.
