---
name: nextjs-auth-session
description: Implement or review Next.js server authentication, session and cookie mutation, protected data access, and authentication redirects. Use for changes to trusted session or authorization boundaries; not feature-local form validation or AppShell-only error presentation.
---

# Next.js Auth and Session Boundaries

Keep authentication, session persistence, authorization, and navigation distinct. A request becomes authorized through the adopted server-side policy, not because a cookie exists, a page rendered, or an early routing check completed.

## Establish the boundary

Before changing behavior, identify from the project:

- Installed Next.js and authentication-library versions; App Router, Pages Router, or mixed ownership; runtime and deployment proxy configuration.
- The session authority: verified stateless credentials, server-side session records, an external authentication service, or an intentional combination.
- Protected reads and mutations, genuinely public routes, optional personalized content, and the server entry points that reach each operation.
- The current credential storage boundary, cookie attributes, expiry/revocation policy, tenant membership source, and allowed post-authentication destinations.

Follow one representative request from entry point through session verification to the protected operation and response. Include direct action/API invocation, not only navigation through the UI. This step is complete when each changed protected operation has an identified authoritative check and its unauthenticated, unauthorized, and unavailable outcomes are distinguishable.

If a missing provider contract changes what constitutes a valid session, identify that missing evidence before changing authentication semantics. Continue with supported boundary fixes; do not invent tokens, identity endpoints, refresh retries, or an outage bypass.

## Preserve the security invariants

### Authentication failure grants no authority

- Missing, expired, invalid, revoked, and unverifiable credentials must not authorize a protected operation. Treat provider unavailability as unavailable, not successful authentication or proof of invalid credentials.
- A genuinely public route may remain available during an authentication outage by its own policy. Optional personalization may fall back to public content when the existing policy permits it; that fallback must not carry protected data or permissions.
- Cookie presence, decoding a token without verification, client state, and a successful login in another request are not substitutes for the adopted verification policy. For stateless sessions, use the library's authenticated verification and claim checks; for stateful sessions, preserve the required session lookup and revocation semantics. Do not force a database onto a valid stateless design.

### Protected operations enforce authorization

- Authorize at the trusted server boundary close to the operation. Server Actions, Route Handlers, API routes, and server-side data access must remain safe when called without the page, layout, or Proxy check.
- Resolve the acting principal from verified session state. Treat resource IDs and requested tenant IDs as untrusted selectors, then check membership, ownership, and operation permission against authoritative policy. Authentication alone does not grant access to another tenant's data.
- Reuse the existing protected data-access seam. A server-only DAL is a useful default for a new App Router application; an established REST/GraphQL backend that authorizes every operation remains valid. Component-local server access in a prototype is not automatically a required architecture rewrite, but still must enforce authorization and return only permitted data.
- UI hiding and layout redirects are presentation, not complete authorization: nested routes and direct actions are separate entry points. Optional Proxy/Middleware checks may provide early routing but cannot be the sole defense for protected operations.
- Return the minimum data the caller is allowed to receive. Keep server-owned credentials and secrets out of Client Component props, browser-readable storage, public environment variables, serialized errors, and logs.

### Session writes use supported response boundaries

Read [framework-boundaries.md](references/framework-boundaries.md) when changing cookie APIs, early routing, redirects, or session-related caching; it distinguishes router and version branches.

- Mutate cookies only in a boundary that can write the actual outgoing response headers before they are sent. A function labeled `use server` does not make a cookie write during Server Component rendering valid.
- Preserve the adopted secure storage policy: HttpOnly for server-owned session cookies, Secure in the deployed HTTPS environment, and SameSite, domain, path, and lifetime appropriate to the authentication flow. Cross-site callback requirements and documented local development behavior are reasons to choose compatible settings, not to relax production credentials indiscriminately.
- Preserve applicable CSRF defenses on cookie-authenticated mutations. Next.js Server Action protections do not automatically cover arbitrary Route Handlers or external APIs. Keep authentication-provider callback protections and narrowly justified trusted-proxy/origin configuration intact.
- Create, rotate, refresh, and revoke sessions through the established authority. Cookie lifetime and server expiry must implement the same policy. Clearing a browser cookie alone is not server-session revocation when that is required; failed revocation must not be reported as confirmed revocation.
- When deleting a cookie, preserve the scope needed to remove the cookie actually issued. Verify the browser's resulting state rather than assuming a method call removed it.

### Redirect policy and redirect control flow are separate

- Validate user-controlled return destinations against the application's adopted policy before passing them to redirect APIs. Parse and resolve the destination against the trusted base, then validate the resulting destination; naive prefix checks can admit protocol-relative or deceptive destinations.
- Same-origin destinations are a common policy, not a universal restriction. Explicitly allowed external identity or product destinations remain valid; preserve their exact trust rules and a safe existing fallback for rejected input.
- Preserve framework navigation control flow. An authentication success followed by a framework redirect must not become an error response because a broad catch swallowed the redirect signal. Prefer narrowing the catch to the fallible operation and redirecting afterward.

### Session and tenant isolation survive caching

- Identify the cache's lifetime and sharing scope before placing session-derived values in it. Request-local deduplication and persistent/shared caching have different authorization consequences.
- Never reuse a cached principal or authorized result across unrelated sessions or tenants. Where shared caching is permitted, authorize first and key the derived data by all required non-secret isolation dimensions; possession of a cache key is not permission.
- Preserve the adopted invalidation behavior on logout, session rotation, membership changes, and tenant switches for the path being changed. A client refresh is not proof that protected server data or another cache was invalidated.

## Apply the smallest supported change

1. Classify the defect as verification, operation authorization, response mutation, destination policy, redirect control flow, or isolation. Identify the observable failure before choosing a new abstraction.
2. Change the existing owner of that behavior and all affected callers. Keep provider/library choices and public-route exceptions unless the task explicitly changes them.
3. If global session-expired UI or root navigation changes are also requested, use `react-app-shell-error-handling` conditionally. Keep that presentation contract separate from the server's authority to return data or mutate state.

The change is ready for verification when every changed entry point reaches its authorization boundary and each session mutation has a supported response owner. This skill does not prescribe an application scaffold, error-dialog host, form library, matcher list, or authentication vendor.

## Verify observable outcomes

Use the project's permitted local execution path and synthetic accounts, not production credentials or real identity submissions. Exercise the affected branches:

- A verified, authorized principal receives the intended result; a direct protected request with missing, invalid, expired, or unavailable authentication receives no protected data and causes no protected mutation.
- A valid principal requesting another tenant's resource is denied, while a genuinely public route remains usable under its public policy.
- Login/refresh/logout produces the intended outgoing cookie headers and browser state; the next request observes the intended session state. Inspect attributes without recording credential values.
- A permitted return destination navigates successfully; a malicious destination follows the adopted rejection/fallback policy. Successful redirect control flow is not converted into an application error.
- If caching changes, two isolated sessions and a tenant/session transition do not receive one another's protected results.

A missing auth service, browser, or runnable application limits the evidence. Report the boundary reasoning and exact unexercised scenario rather than claiming runtime verification. Finish with changed boundaries, preserved exceptions, observed outcomes, and remaining uncertainty.

## Sources and applicability

This skill combines the authoring request's security criteria with the primary Next.js guidance below. Its workflow, counterexamples, and verification scenarios are author-derived applications of those criteria, not claims of experiments already run.

- [Next.js authentication](https://nextjs.org/docs/app/guides/authentication): session models, optional early checks, protected data access, actions, and handlers.
- [Next.js data security](https://nextjs.org/docs/app/guides/data-security): existing external-API architecture, server/client data boundaries, and mutation security.
- Version-sensitive API sources and their applicability are in the conditional framework reference. Consult the installed version and provider documentation before applying newer API behavior.
