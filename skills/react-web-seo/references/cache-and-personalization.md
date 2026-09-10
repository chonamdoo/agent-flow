# Public Cache and Personalization

Read when public HTML/metadata/RSC depends on auth, tenant, locale, runtime request data, cache keys, revalidation, or CDN behavior. Preserve the skill's public-response and security contracts.

## Scope Before Speed

Trace what is cached and who can receive it: request-local memoization, persistent data cache, route/HTML output, RSC/client router cache, and CDN are different layers. A request-local deduplication guarantee does not establish safe cross-request caching, and a client transition is not equivalent to a cold anonymous request.

- Classify each value and result as genuinely public or scoped to a trusted actor/tenant/session and other relevant variants. Include metadata and streamed RSC data, not just visible HTML. Hidden markup is still disclosed data.
- Keep authentication/authorization at the trusted boundary. A cache key, locale, cookie, or bot classification cannot grant permission. When sharing scoped results is intentional, key and invalidate at the complete scope and retain authorization checks; otherwise keep the result request-local/private or uncached.
- Check server and CDN cache behavior together, including cache-control and variant handling. Do not assume a cookie read alone prevents every downstream layer from sharing output.
- Invalidation/revalidation must preserve both freshness and access boundaries after logout, tenant switch, publication changes, or permission changes. An update to body data that leaves private or stale metadata behind is incomplete.

Sharing a public catalog entry across users is normal. Adding an unscoped user name, private draft title, or account-specific RSC fragment to the same cached response is a concrete leak, even if the screenshot looks public.

## Next Version Branch

Inspect installed Next version and whether Cache Components is enabled before choosing APIs or defaults. Current Cache Components documentation uses `use cache`/lifetime and streaming boundaries; projects without that setting use a different caching model. This skill does not enable Cache Components or copy a current fetch/cache option onto every request.

Request memoization across page/metadata reads can reduce duplicate work without being a durable shared cache. Persistent caching of personalized results requires a supported scope and invalidation design; do not lift cookies/headers into a publicly reused value merely to satisfy a cache API restriction. Follow the installed API's serialization and runtime-data constraints. Where this changes composition or data boundaries, apply the existing React architecture contract rather than moving raw clients into feature Context.

## Focused Evidence

Exercise the affected request path as user A, user B, and anonymous, including cache misses and hits and both direct loads and relevant client transitions. Inspect HTML, metadata, response headers, and RSC payloads where present. Use intentionally distinct non-secret fixtures to identify cross-user values. Show publication/revalidation behavior for the changed resource when applicable. A local mock without the actual CDN cannot prove deployed edge isolation; identify that evidence limit rather than marking it verified.

## Primary Sources

- [Next caching with Cache Components](https://nextjs.org/docs/app/getting-started/caching).
- [Next caching without Cache Components](https://nextjs.org/docs/app/guides/caching-without-cache-components).
- [Next use cache constraints](https://nextjs.org/docs/app/api-reference/directives/use-cache), [React cache request lifetime](https://react.dev/reference/react/cache).
- [HTTP caching and personalization](https://developer.mozilla.org/en-US/docs/Web/HTTP/Guides/Caching).
