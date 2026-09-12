# next-intl Environments and Messages

Read this branch only for an affected `next-intl` integration. Check the installed Next.js and `next-intl` versions and whether the project uses App Router or Pages Router before applying examples from current documentation. This reference summarizes first-party documentation consulted on 2026-09-11; it does not require a framework upgrade or prescribe a filesystem layout.

## Match the Execution Environment

- Async Server Components use awaitable APIs such as `getTranslations` from `next-intl/server`; do not call hooks from an async component.
- Non-async shared components can use `useTranslations` from `next-intl` in supported server or client environments. The fact that it is a hook does not make this library API client-only. Which environment a shared component executes in depends on its import boundary.
- Keep translated content on the server when the client only needs labels or children. Passing processed labels, selected messages, or a whole catalog are all supported choices; choose by actual interactivity and payload needs, not a rule that all translations must run in effects or all catalogs must be split.
- Server configuration uses `getRequestConfig` and is evaluated with request-based caching. Keep request-specific resolution there rather than mutating a global current locale. Follow the installed version's supported locale-resolution API; current routing examples are not grounds to rewrite a working older setup.
- A `NextIntlClientProvider` rendered from a Server Component can inherit `locale`, `messages`, `now`, `timeZone`, and `formats`. This does not imply automatic inheritance from an arbitrary CSR root or a Pages Router setup: provide configuration through that environment's supported integration.
- `messages={null}` opts out of sending messages through that provider; client components calling translation APIs still need the appropriate messages from a supported provider boundary. A server-translated label consumer does not need a duplicate client catalog.
- Nested providers inherit configuration, but individual props are atomic: supplying a `messages` prop does not automatically deep-merge a namespace subset into the parent's messages. Merge deliberately when the intended lookup contract requires it.
- `onError` and `getMessageFallback` functions are not automatically inherited across the server-to-client boundary. Define needed client error behavior in a client-side provider rather than trying to serialize function props from the server.

## Distinguish Message Absence from Rendering Failure

`next-intl` permits merging messages from a fallback locale into the current catalog. Apply the product's allowed fallback language and precedence before supplying messages; preserve compatible current-locale values, including nested entries.

`t.has` checks whether an optional message is available in the current message configuration. It is not a transport-status or catalog-version check. `getMessageFallback` customizes the error case; it is not by itself a complete fallback-locale catalog loader. Preserve `onError` classification for malformed or failed formatting rather than reporting every failure as an expected absent translation.

Fallback configuration must produce compatible first server and client output. A server-only error callback does not guarantee the client will render the same error presentation; inspect both environments when that path changes.

## Preserve the Message Format and Trust Boundary

- `t` formats ordinary messages, including ICU plural/select and interpolation. Keep arguments and their meaning compatible with the consuming component when remote catalogs change.
- `t.rich` maps supported message tags to callbacks returning React content. Application-controlled components retain tag behavior and attributes; translation text supplies the grammatical placement.
- `t.markup` accepts and returns strings through markup callbacks. It produces markup, not a safe React element tree or a sanitization guarantee.
- `t.raw` bypasses message parsing and may return any valid JSON value, not necessarily a renderable string. Validate the expected shape at the delivery boundary. Raw HTML passed to `dangerouslySetInnerHTML` still requires sanitization.
- A translated link destination remains untrusted even when the text uses `t.rich`. Apply the existing URL policy before assigning message-derived data to navigation attributes.

## Primary Documentation

- [Server and Client Components](https://next-intl.dev/docs/environments/server-client-components): async APIs, shared components, server-translated labels, and client message-delivery options.
- [Request configuration](https://next-intl.dev/docs/usage/configuration): provider inheritance, request caching, formatting inputs, error callbacks, and fallback catalog merging.
- [Rendering translations](https://next-intl.dev/docs/usage/translations): ICU messages, `t.rich`, `t.markup`, `t.raw`, sanitization, and optional-message lookup.

If a specific API is absent from the installed release, retain the common consistency and safety contract and use that release's documented equivalent. Report the version-specific gap when no supported equivalent is established; do not invent a helper or silently migrate the project.
