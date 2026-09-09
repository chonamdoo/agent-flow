# Next App Router Metadata and Bots

Read only when the affected public route uses Next App Router metadata, streaming, bot-specific responses, or not-found behavior. Check installed Next version/router/configuration; current documentation is not a minimum supported version or a Pages Router migration instruction.

## Server Ownership

- `metadata` and `generateMetadata` exports belong to Server Components. Keep server data fetching, metadata, and composition in the Server page/layout; move only interactive state/effects into a Client wrapper. A server-only view needs no blank ViewModel hook or client conversion.
- RSC inputs to Client Components must satisfy React's supported serialization contract; supported Server Action references are different from arbitrary functions. Do not pass server repository/client objects through a Client Context provider. Read `react-clean-presentation-architecture` when changing those boundaries.
- Use static metadata when sufficient; use `generateMetadata` for actual dynamic needs. Inspect parent inheritance, file-based overrides, `metadataBase`, and final absolute URLs rather than assuming the local metadata object is the final head.
- Nested metadata is shallowly merged: assigning a child's `openGraph` can replace inherited nested fields. Preserve the intended fields explicitly where necessary, not by copying every parent's object unconditionally. Route titles/canonical/locale/social images must remain aligned with the actual resource.

## Streaming and Status

Current Next documentation distinguishes JavaScript-capable bots, such as Googlebot, from HTML-limited bots, such as `facebookexternalhit`. Dynamic metadata can stream after the initial UI and be appended to the body; for HTML-limited bots Next can wait and include metadata in the head. Verify the installed behavior and actual user-agent configuration. Requiring identical initial heads for every bot would reject valid streaming.

`htmlLimitedBots` overrides change the built-in detection policy. Do not replace its defaults wholesale as a generic SEO optimization; establish which crawler requires a change and inspect that response. User-agent spoofing can exercise the response branch locally but cannot reproduce the search engine's entire pipeline.

A `notFound()`/not-found component is not proof of an HTTP 404: Next's streamed not-found responses can be 200 after headers commit, while non-streamed responses can be 404. Inspect status plus robots/indexing output and define the missing-resource policy. If an exact status is a requirement, resolve existence early enough in the actual delivery path rather than trusting the component name.

## Focused Evidence

Observe the public route on first request and client navigation where relevant. Compare a valid unvisited slug, a missing slug, parent/child metadata overrides, browser and affected bot variants, and the final response stream. Confirm that metadata work did not force a formerly server-only page into the client bundle or introduce hydration/serialization errors. Valid browser-specific streaming is the normal control; unavailable main content or conflicting canonical output is a defect.

## Primary Sources

- [generateMetadata, merging, streaming](https://nextjs.org/docs/app/api-reference/functions/generate-metadata).
- [htmlLimitedBots](https://nextjs.org/docs/app/api-reference/config/next-config-js/htmlLimitedBots), [not-found response status](https://nextjs.org/docs/app/api-reference/file-conventions/not-found).
- [Server and Client Components](https://nextjs.org/docs/app/getting-started/server-and-client-components), [React serializable values](https://react.dev/reference/rsc/use-client#serializable-types-returned-by-server-components).
