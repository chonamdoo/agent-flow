---
name: react-tanstack-query
description: React Web TanStack Query implementation and review of query identity, QueryClient lifetime, mutation/read synchronization, session transitions, persistence, and SSR hydration. Not TanStack Form alone, generic React state, server-only fetching without Query, or React Native-only work; Zustand-only lifetime and persistence use the standalone conditional reference.
workflowPhases: [design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [TanStack Query, tanstack-query, '@tanstack/react-query', React Query, 탄스택 쿼리]
---

# React TanStack Query

Use for actual React Web Query code or an explicit Query adoption decision. Preserve a working RSC/framework-only or client-only data path; this skill does not authorize adding Query, Zustand, a form engine, or a new application scaffold.

## Establish the Data Contract

1. Read manifests, lockfiles, installed exports/types, and existing query adapters. Identify React, Query/core, framework/router, and any persistence adapter versions. The pinned API evidence below is **v5.89.0**, not a minimum-version requirement. Rolling documentation may use different exports or signatures; retain valid installed APIs rather than copying newer examples.
2. Follow the changed read or mutation from its consumer through the existing API boundary. Identify result shape, HTTP/business errors, credentials, data identity, QueryClient creation/provider, and any server or persisted cache. Browser and server execution can use different transports while representing the same resource.
3. Decide which owner supplies each displayed server snapshot and which operation refreshes it. When a Query result feeds a form, read the actual form skill conditionally below instead of redefining draft/reset policy here.

Discovery is complete when the chosen API is available in the installed release and each changed result has an identifiable cache, consumer, and refresh owner. Missing backend or runtime access limits the claim; do not infer a server contract from a mock or a TypeScript type.

## Query Identity and Read Results

- **Keys describe data.** Use serializable array keys with every changing dimension that distinguishes the returned data: record, filters, pagination, locale, or non-secret principal/tenant scope where relevant. Keep ordinary and infinite-query shapes distinct. Reuse the project's key/options convention; a key-factory library is not required.
- **Scope is not a credential.** A public catalog may legitimately share a key across users. Protected results need non-secret isolation dimensions within a shared client, or an appropriately isolated client lifetime, plus the transition rules below. Never place access tokens or other secrets in keys. A key does not authorize a request.
- **Match the function contract.** A compatible function reference, closure over keyed variables, or `QueryFunctionContext` consumer can all be valid. Query passes context, not an arbitrary record ID: wrap an API function expecting an ID or options when those parameters differ. Return the data promise, resolve an intentional empty result such as `null` where appropriate, and reject failures rather than caching an error as success. A supported default `queryFn` is also valid.
- **Handle HTTP failure at its existing owner.** Native `fetch` resolves HTTP error responses; check the status/`response.ok` and reject according to the API contract before treating the body as success. If the project's adapter already rejects and maps errors, reuse it instead of duplicating that policy. Handle business-failure payloads according to their established contract, not the HTTP status alone.
- **Cancellation has limits.** Pass Query's signal through compatible read transports when cancellation is needed. Unmounting alone does not guarantee a request is cancelled or its result discarded. Check installed hook/adapter limitations, especially Suspense cancellation, and retain identity guards for effects outside the query cache.

## QueryClient Lifetime and Cache Operations

Keep the browser client stable for its intended application or session lifetime, including rerenders and initial suspension. A browser-only module instance or a correctly scoped provider instance can be valid. A lazy React initializer is not sufficient if initial suspension discards it before a protecting boundary; inspect the actual tree. Deliberate session-client replacement is different from accidental recreation on each render.

Keep server QueryClients request-isolated for user-derived data. A new prefetch client per server boundary or a genuinely request-scoped shared client are both valid; a mutable server module singleton is not request isolation. A separately designed public server cache may remain shared under its public-data contract.

Choose operations by their effect, not their names:

| Need | Query v5 behavior to account for |
| --- | --- |
| Refresh stale server snapshots | `invalidateQueries` marks matches stale and normally refetches active queries; it does not erase their data. Choose the matching scope and refetch policy deliberately. |
| Dispose cache entries | `removeQueries` removes matching queries. It does not by itself establish a complete session transition for mounted consumers and other asynchronous work. |
| Return to an initial query state | `resetQueries` can restore `initialData` and refetch active queries; this is not private-data erasure. |
| Dispose a whole owned cache | `clear` clears the client's caches. Use only when that whole lifetime is being discarded; it is neither a universal logout rule nor server-write cancellation. |

`staleTime` controls freshness, while `gcTime` in v5 controls inactive retention; neither is an authorization or logout boundary. Older versions may use other names. Apply `setQueryData` updates immutably and only to the intended identity; a whole-object replacement is safe only when the response really supplies that snapshot.

## Mutation Acceptance Versus Read Synchronization

Separate the **write outcome** from the **subsequent read outcome**. Interpret business acceptance through the existing API/action contract. When a caller needs the completion promise, use the installed `mutateAsync` contract or the existing equivalent; `mutate` is not an awaitable save result.

After confirmed acceptance, update the relevant cache from an authoritative response or invalidate/refetch the affected keys. Await synchronization when the UX depends on it, but keep a failed refresh distinguishable from a rejected write. For example, a saved record with a failed list refresh needs refresh recovery, not resubmission of the write. In v5, refetch/invalidation promises do not reject on refetch failure by default; inspect `throwOnError` and resulting query state before claiming synchronization succeeded. Awaited mutation callbacks can also affect the caller's completion/error path; do not let one undifferentiated catch label a confirmed write as failed.

Bind optimistic updates, rollback, cache writes, navigation, and error feedback to the submitting identity and request. A late rollback must not overwrite newer accepted data. Callback placement matters: hook-level and per-`mutate` callbacks have different behavior on unmount and consecutive mutations. Check the installed signatures and existing orchestration before relying on callback order.

An aborted, timed-out, or disconnected write can have an unknown server outcome. Cache cleanup cannot roll it back. Preserve the application's reconciliation/idempotency policy before replaying it, including paused or persisted mutations; do not add automatic write retry as refresh recovery.

## Identity Transitions and Persistence

For logout, account/tenant switches, or changed access scope, distinguish **address separation** from **disposal and display policy**. Inspect the entire affected lifetime:

1. Stop old consumers from displaying or starting work under the new identity. Mounted observers, retained layout trees, placeholder/previous data, and local snapshots may outlive a key change. Gate or rebind them deliberately; disabling fetching does not erase their existing data.
2. Isolate old reads and side effects. Cancel relevant reads where supported and ensure late results cannot populate the new identity's view. Correlate mutation callbacks and other cache writes with the original client/scope and current generation; changing a global credential while old closures still run is not isolation.
3. Apply the chosen disposal policy: replace and detach a session-owned client, or prove scoped cleanup covers every private query and affected consumer in a retained client. Preserve genuinely public caches when policy allows. Neither `clear()` everywhere nor adding a principal to the key is a complete transition by itself.
4. If persistence exists, cover in-memory data, storage namespaces, subscriptions, pending saves, and asynchronous restoration. An old restore or delayed storage write must not recreate private data after cleanup or enter the new identity's client. Check permitted persisted fields, schema/buster, expiry and privacy policy, and paused-mutation replay. `PersistQueryClientProvider` can coordinate restoration and query fetching in supported versions; it does not decide identity or retention policy for the app.
5. Where Next.js/server caching also participates, trace its identity and invalidation separately using `nextjs-auth-session`. Clearing Query or refreshing the client does not prove a protected server cache was invalidated.

This transition is ready for verification only when the old observer, in-flight read, mutation callback, and restore/write paths present in the app have an explicit disposition. Use existing lifetime mechanisms; do not introduce a second session framework.

## SSR, Hydration, and RSC Branches

Retain the smallest supported rendering strategy:

- **Client-only queries:** no server prefetch or hydration boundary is required for data intentionally fetched after client rendering.
- **Server snapshots via `initialData`:** valid when its cache initialization and freshness tradeoffs fit. It does not overwrite an existing query merely because a newer prop arrives; preserve the actual fetched timestamp when needed through supported options.
- **Prefetch/dehydrate/hydrate:** transfer only data permitted for that client, align keys and data shapes, and place hydration boundaries around the consumers that need the transferred state. A boundary may be in a layout or nested subtree; there is no universal requirement for one per route. Check serialization, error exposure, pending-query support, and streaming behavior against the installed framework and Query release.
- **RSC/framework-only data:** retain it without Query where client cache behavior is unnecessary. If RSC and Query both render the same changing entity, name how their displayed snapshots stay coherent; client invalidation does not automatically revalidate RSC output.

Judge hydration freshness from the original fetch time, elapsed time until hydration, effective `staleTime`, invalidation, and refetch settings. A positive `staleTime` is not sufficient to guarantee no immediate refetch. Confirm server HTML and first client output agree; `'use client'` does not mean browser-only execution.

## Conditional References

- Zustand store lifetime, SSR initialization, persisted client state, or draft checkpoints: [Zustand lifetime and persistence](references/zustand-state.md). This reference can be read directly for Zustand-only work, without Query installation or loading the rest of this skill.
- TanStack Form draft initialization, refetch/reset, or submit integration: read `react-tanstack-form`, particularly its lifecycle and subscriptions reference. For RHF, read `react-hook-form-zod` and its lifecycle reference. Query usage alone selects neither form engine.
- Next.js request destinations, rewrites, or browser/server transport differences: read `nextjs-api-routing`; trusted authentication and server-cache isolation changes use `nextjs-auth-session`.

## Completion Evidence

For implementation, exercise the changed path through the project's authorized runtime and declared checks. Observe rendered results, network outcomes, and affected cache/lifetime state rather than compilation alone. Select only relevant scenarios: a key-changing filter, HTTP rejection, an accepted write followed by failed refresh, an old read/mutation completing after identity change, reload during restoration, or server/client hydration and revalidation.

For review, report the concrete key/function/client or callback path supporting each finding, the observable harm, and a valid alternative where applicable. Completion requires an inspectable identity/lifetime decision for every changed cache path and separate evidence for write acceptance and read synchronization. Report executed evidence, source reasoning, and unexercised runtime cases separately; mock success is not provider or deployment proof.

## Primary Sources and Applicability

- [Query keys](https://tanstack.com/query/latest/docs/framework/react/guides/query-keys) and [query functions](https://tanstack.com/query/latest/docs/framework/react/guides/query-functions): identity, context, compatible function shapes, and rejected errors.
- [Mutation lifecycle](https://tanstack.com/query/latest/docs/framework/react/guides/mutations): completion, callback placement, retries, and persistence; signatures are version-sensitive.
- [QueryClient v5.89.0](https://github.com/TanStack/query/blob/v5.89.0/docs/reference/QueryClient.md) and [cancellation v5.89.0](https://github.com/TanStack/query/blob/v5.89.0/docs/framework/react/guides/query-cancellation.md): operation effects, error propagation, and cancellation limits.
- [SSR v5.89.0](https://github.com/TanStack/query/blob/v5.89.0/docs/framework/react/guides/ssr.md), [advanced SSR v5.89.0](https://github.com/TanStack/query/blob/v5.89.0/docs/framework/react/guides/advanced-ssr.md), and [persistence v5.89.0](https://github.com/TanStack/query/blob/v5.89.0/docs/framework/react/plugins/persistQueryClient.md): client lifetime, rendering alternatives, and restoration races.

The identity-transition and write/read recovery criteria are engineering applications of these APIs and the application's security contract, not built-in guarantees of Query. Official examples and rolling documentation are not evidence that an installed application has exercised those paths.
