---
name: react-runtime-i18n
description: Design, fix, or review React Web runtime or remote translation delivery, SSR/CSR locale-message consistency, compatible fallback and cache invalidation, and safe rich-text rendering. Not plain copy editing, React Native library selection, or a mandate to replace an existing i18n architecture.
---

# React Runtime Internationalization

Keep the active view's content, locale, messages, and formatting inputs coherent as translations arrive or change. Preserve the application's rendering architecture and its authorized fallback policy. Runtime translation does not mean translating only after mount.

## Establish the Delivery Contract

Inspect the installed framework and i18n library versions, rendering mode, locale resolution, message loading, cache keys, rich-text boundary, and existing error/fallback policy before editing. Distinguish client-only rendering, hydrated SSR, and Server Components; use the framework's existing data and provider boundaries.

Identify the requested locale and how it is resolved, the message/content identity and revision or compatibility rule, the consumer's message format, the relevant formatting inputs, and any request/tenant scope. Find who owns loading, fallback, and activation of a new locale. Establish the expected visible result for an absent key, unavailable bundle, malformed message, unsupported locale, and interrupted locale switch when those cases are affected.

If a fallback policy is unknown, preserve documented existing behavior and identify the missing decision before changing language or failure semantics. Do not invent a brand's supported locales, regional restrictions, publishing vendor, or translation approval policy. This step is complete when a response can be judged applicable to the current view without relying only on its arrival time.

## Identity and Consistent Rendering

- Keep stable content IDs, message keys, route identity, and error codes separate from translated labels. Translate presentation without changing domain identity or caching content under its displayed sentence. Preserve an adopted message-key scheme unless changing it is in scope.
- For hydrated UI, server output and the initial client render must agree on effective locale, the messages they render, and formatting inputs such as time zone, reference time, and number/date formats. They need not ship identical complete catalogs; server-only translated labels and a client subset are valid when the rendered output agrees.
- Resolve request-specific locale configuration within the request boundary. A mutable process-wide current locale or translator can leak one request's language or messages into another. Sharing immutable public catalogs is valid when the cache identity distinguishes all relevant variants.
- Treat a locale switch as a coherent transition. Keep the prior coherent view, display the established loading state, or use the authorized fallback while the next catalog loads. Do not label the view with the new locale while silently rendering an unrelated old catalog.
- Client-only translation and intentional post-hydration personalization are valid when required by the architecture. For a hydrated boundary, their initial placeholder or fallback must still agree with the server. Do not force every application into SSR, nor move working server translation into effects to hide a mismatch.
- Update document or region language and direction consistently with the rendered experience where affected. Keep semantic markup, accessible labels, plural/select logic, and formatted values meaningful across locale changes rather than translating only visible text nodes.

When the affected project uses `next-intl`, read [next-intl environments and messages](references/next-intl-environments-and-messages.md). Other libraries retain their documented APIs; using this skill does not require adopting `next-intl`.

## Delivery, Cache, and Fallback

- Bind loaded messages to their locale, content/namespace, revision or compatibility contract, and any genuinely varying request or tenant scope. Keep version compatibility explicit without imposing a new version field on a service that already guarantees compatible catalogs another way.
- A late response may populate its correctly scoped cache, but may activate only if it still matches the active request/view. Cancellation can reduce obsolete work; identity checks at application prevent a completed older request from replacing newer state.
- Invalidate or replace entries when their compatibility no longer holds. Do not combine new consumers with cached messages that changed required arguments, rich-text tags, or key meaning. A last-known-good catalog is a valid outage fallback only within the approved scope and compatibility policy.
- Distinguish a missing key, unsupported locale, transport failure, authorization failure, malformed payload, and message-format failure. Preserve the original failure category/cause at the existing error boundary even when user-facing fallback succeeds. Do not translate an upstream denial or outage into a false successful fetch or a generic missing-key diagnosis.
- A configured fallback language is valid. Resolve it deliberately for the requested view and compatible message contract; it is not the same as accepting an arbitrary cache hit or a response belonging to another request. Missing translations do not automatically require failing the whole screen.
- When catalogs are merged, apply the documented fallback precedence and nested-key semantics so current-locale messages win where present and compatible fallback fills permitted gaps. Define missingness using the contract; an intentional empty string is not necessarily missing. Do not assume shallow object spread preserves nested fallback keys.
- Separate message fallback from failure presentation. A local placeholder, optional-content omission, or bounded component error can be appropriate under policy; forcing a global error screen for every absent message is not a general i18n rule.

Translation configuration does not grant access rights. Keep delivery credentials and private catalogs server-side unless intentionally authorized for the client. Publishing catalogs, uploading source content to a vendor, installing a dependency, or deploying changes requires existing authorization; this skill does not grant it. If common global error UI is affected, use `react-app-shell-error-handling` conditionally rather than inventing a second AppShell owner.

## Rich Text Is a Trust Boundary

Prefer the library's structured message format: translate complete grammatical units, preserve ICU arguments or equivalent placeholders, and map allowed rich-text tags to application-controlled React elements. Translators can reorder meaningful spans without gaining control over executable markup or component behavior.

Treat translation-origin HTML and message-supplied URLs as untrusted. Ordinary text, parsed rich text, and raw HTML have different contracts. A library method named `raw` or `markup` is not a sanitizer. When raw HTML is genuinely required, use the existing vetted sanitization boundary and an explicit permitted-markup policy before rendering; validate URL schemes and destinations according to the application's link policy. Preserve meaningful links, emphasis, and accessible structure rather than deleting every tag as a blanket workaround.

## Observable Verification

Use the authorized application runtime with synthetic or permitted content. Select cases for the paths changed and record the active identity and visible result, not merely a translator call or a fetched JSON object.

- For SSR changes, inspect the direct response and initial hydrated output under the same locale and formatting inputs, including a cold request. Check for hydration errors and visible language/date/number disagreement. A build succeeding does not establish consistency.
- Switch between supported locales with responses finishing out of order. Only the currently applicable result may replace the view. Exercise cache hits as well as misses, and concurrent independently scoped requests where server/request caching changed.
- Omit a permitted translation and observe the configured fallback succeeding. Separately exercise an incompatible revision, wrong-locale/request response, or malformed message; these must not enter the active view merely because missing-key fallback is allowed.
- Render pluralized and rich-text content for the affected message contract. Exercise untrusted markup or unsafe link input at the actual rendering boundary; the result must preserve permitted semantics without executing injected content.
- Observe the affected outage or loading path. A usable fallback may remain visible while the original failure meaning remains available through the existing error handling. Do not introduce external telemetry solely to satisfy this check.

Derived counterexamples: server-translated labels passed to interactive components need no client translator; a documented default-language fallback may fill a missing message; an immutable public locale catalog may be shared between requests. Critical defects are cross-request contamination, incompatible message activation, hydration disagreement, unsafe HTML, or loss of a legitimate fallback—not the absence of a specific provider or effect.

Report framework/library versions, cases actually exercised, and unavailable server/browser/delivery evidence separately. Source review cannot prove runtime race isolation or injection resistance. A delivery service being inaccessible does not authorize fabricated responses as production evidence or a live upload.

## Sources and Applicability

The following first-party documentation was consulted on 2026-09-11; it is living guidance, not a release pin. Request identity, compatibility, and fallback distinctions above are reusable engineering judgments. The derived examples illustrate those judgments and are not claims of executed experiments.

- [React: hydrateRoot and matching server/client output](https://react.dev/reference/react-dom/client/hydrateRoot)
- [next-intl: Server and Client Components](https://next-intl.dev/docs/environments/server-client-components)
- [next-intl: Request configuration and fallback messages](https://next-intl.dev/docs/usage/configuration)
- [next-intl: Translations, rich text, raw messages, and optional messages](https://next-intl.dev/docs/usage/translations)
