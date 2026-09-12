# Framework-Specific Authentication Boundaries

Read the applicable section when changing cookie writes, early routing, redirect handling, or authenticated caching. These are API distinctions, not a requirement to migrate routers or enable newer features.

The linked Next.js pages were consulted on 2026-09-11 and identified their documentation version as 16.3.4. That is a documentation snapshot, not a minimum supported version or proof about the consuming application's installation. Verify older-version behavior against the installed release and its authentication adapter.

## Cookie access and mutation

[The cookies API](https://nextjs.org/docs/app/api-reference/functions/cookies) describes `cookies()` as asynchronous in current App Router releases. Next.js 14 and earlier used synchronous access; Next.js 15 retained transitional synchronous compatibility. Follow the installed API rather than treating that transition as a permanent guarantee.

| Execution boundary | Appropriate operation |
| --- | --- |
| App Router Server Component render | Read the incoming session; do not set or delete cookies during rendering. |
| App Router Server Action or Route Handler | Read the incoming session and set/delete cookies on the outgoing response before headers/streaming commit. Use the authentication adapter's supported mutation path when it owns the session. |
| Proxy or older Middleware | Use the supported request/response cookie API for that version. Changes to a request cookie view are not by themselves browser persistence; send the intended response cookie and retain it on the response actually returned. |
| Pages Router API route or server-side response | Use that router's response API and the adopted authentication library. Do not transplant `next/headers` examples or require an App Router migration. |

Calling a session-refresh helper from a render path does not change the caller's mutation permissions. Separate reading/verifying from a permitted renewal response if the library requires that distinction. Do not suppress a framework write error and claim refresh succeeded.

Deletion has domain/protocol restrictions in the documented `cookies().delete` API. The emitted deletion must also target the issued cookie's name and scope. Same-name cookies on different paths are not interchangeable.

## Early routing is optional and version-sensitive

[The Proxy reference](https://nextjs.org/docs/app/api-reference/file-conventions/proxy) documents the current Proxy convention, its Node.js runtime, request/response APIs, and matchers. Older projects may legitimately use Middleware with a different runtime and library compatibility constraints. Establish the version before renaming files or importing a Node-only session dependency into an older Edge boundary.

Use early checks for the adopted routing policy; avoid repeating expensive authoritative lookups on every prefetched route merely because a Proxy exists. Keep operation authorization at its trusted owner. A matcher change can alter coverage, including the route receiving a Server Action POST; a skipped matcher must not bypass operation security.

Public static resources, login/callback routes, and genuine public pages need their own intended routing behavior. Do not paste a sample matcher or a universal protected-route list into a project. If delivery of otherwise shared static content is access-controlled, verify its delivery boundary explicitly rather than assuming a request-time DAL runs for a built artifact.

## Redirect control flow

[The redirect API](https://nextjs.org/docs/app/api-reference/functions/redirect) documents that `redirect()` throws to terminate the current route segment; it is not an ordinary successful return value. Keep it outside generic error-catching blocks where possible. If a wrapper must catch it, use the framework/library's supported propagation mechanism for the installed version, not an invented private error-shape check.

The API accepts absolute as well as relative URLs. That acceptance is not destination authorization. Apply the application's return-URL policy separately.

The documented wire behavior depends on context: streaming may emit a client redirect through a meta tag; a progressively enhanced Server Action form submission uses a 303 response; other documented HTTP redirect contexts use 307. Do not test every context by expecting the same status code. Verify the actual navigation and method behavior of the changed entry point.

## CSRF and trusted proxies

[Next.js data security: allowed origins](https://nextjs.org/docs/app/guides/data-security#allowed-origins-advanced) documents Server Actions using POST and comparing `Origin` with `Host` or `X-Forwarded-Host`. The supported `serverActions.allowedOrigins` configuration accommodates justified reverse-proxy deployments. Verify the actual trust chain and installed configuration shape; adding broad origins to silence an error can weaken the boundary.

These framework protections do not authorize the acting user, validate action parameters, or establish CSRF protection for every custom endpoint. Preserve the authentication library's callback/state protections and the application's endpoint-specific CSRF strategy. A SameSite setting must fit the intended cross-site flow; it is not a substitute for examining the flow.

## Authenticated caching

Request-scoped React memoization in the [authentication guide](https://nextjs.org/docs/app/guides/authentication#creating-a-data-access-layer-dal) avoids duplicate reads during a render pass. It is not the same as a persistent global session cache.

When the project actually enables Cache Components, consult [authentication with Cache Components](https://nextjs.org/docs/app/guides/authentication-with-cache-components). In that documented model, session access is request-time work, and ordinary `use cache` scopes cannot read `cookies()`. Separate verified, non-secret identity inputs from cacheable derived data and retain authorization at the operation boundary.

The guide also describes `use cache: private`; do not introduce it solely for an auth fix or assume it is available/stable in another release. Follow the installed feature's support, configuration, lifetime, and invalidation semantics. Browser-private caching never replaces fresh authorization for a mutation, and neither cache keys nor tags should contain credentials.
