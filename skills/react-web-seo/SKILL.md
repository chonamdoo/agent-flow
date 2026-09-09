---
name: react-web-seo
description: React Web public-route indexing, metadata/canonical, JSON-LD/sitemap, crawler response differences, and SEO-related public versus personalized cache boundaries. Not every route or TSX change, authenticated internal UI, general cache tuning, ranking guarantees, or React Native-only work.
workflowPhases: [design, implement, implement-fix, red, green, refactor, fix-loop, review, final-review, multi-review, architecture-review, pr-comment-fix, pr-ci-fix]
taskTerms: [React SEO, web SEO, generateMetadata, metadataBase, JSON-LD, canonical URL, noindex, hreflang]
---

# React Web SEO

Apply to public, search-facing React Web URLs and their actual delivery contract. Identify the target routes, intended indexability, relevant crawlers, installed framework/router version, and hosting/cache setup. Preserve the existing framework: Google can render JavaScript, but not every bot can. Choose CSR, prerendering, SSR, SSG, or ISR for the required public experience rather than forcing an authenticated SPA to migrate to Next.js.

## Public Response Contract

- Inspect the direct URL response and rendered result: meaningful main content, crawlable internal `<a href>` links, title/description, canonical URL, applicable hreflang/social metadata, and actual HTTP status/redirect destination. A blank initial shell or correct browser tab title alone does not prove crawler access to content.
- Keep server/initial and rendered canonical signals consistent, including origin, locale, query/duplicate handling, and the final destination. Metadata should describe the visible resource, not stale or unrelated cached content. Use the framework's title/head management; no duplicate `document.title` effect is required.
- Distinguish robots.txt crawl instructions, noindex indexing instructions, and authentication/authorization data protection. A robots-blocked URL may prevent a bot reading noindex; neither mechanism protects secrets or private records.
- Missing, removed, redirected, or unauthorized resources need intentional delivery semantics, not a successful content page containing only an error message. Inspect the real status, not just a `notFound` call or error component. Streaming may already have committed the status.
- Sitemap entries are public canonical URLs with truthful modification times. JSON-LD represents visible, supported facts; do not invent reviews, ratings, availability, or other structured-data claims.

## Security and Authority

- Public/shared caching must not expose A's HTML, metadata, RSC payload, or personalized data to B or an anonymous request. Establish auth/tenant/user scope and invalidation before caching; bot user-agent selection is not an authorization mechanism.
- Serialize JSON-LD safely in the HTML script context. `JSON.stringify` alone does not prevent a data value containing `</script>` from escaping the script element. Use the existing safe serializer, or a verified script-safe encoding such as replacing `<` in serialized JSON with the literal `\u003c` sequence. Inspect the produced HTML, not only the object or TypeScript type.
- SEO work does not authorize a deployment, external write, dependency installation, public submission, or access-control bypass. Keep runtime approval and the active workflow's evidence gates intact.

## Conditional Detail

- Next App Router metadata, streaming, bot-specific output, or not-found behavior changes: read [Next and bots](references/next-and-bots.md). A non-Next public SPA does not need this branch.
- Public-response personalization, request data, revalidation, Cache Components, or CDN/cache-key changes: read [Cache and personalization](references/cache-and-personalization.md). A title-only change without a caching boundary change need not load it.

## Observable Acceptance

Use the authorized project runtime and declared commands. For affected routes, observe a cold direct request to an unvisited public slug without a warm client cache or login, its main content and links, final metadata/canonical, and a missing URL's status/indexing treatment. When bot delivery changes, compare a normal browser, a JavaScript-capable crawler, and an HTML-limited crawler under the actual streaming policy. When JSON-LD changes, exercise an adversarial script-ending string. When caching changes, inspect A, B, and anonymous responses through both cold and hit paths, including metadata/RSC where present.

Report the actual HTTP/HTML and rendered observations separately from post-deployment Search Console indexing evidence. Local rendering, a successful build, or a spoofed user-agent is not proof of Google indexing, ranking, or timing; do not promise those outcomes. Report inaccessible deployment-only evidence rather than inventing it.

Normal counterexamples: an authenticated CSR dashboard can remain CSR; a cache containing only genuinely public data may be shared; bot-appropriate streamed metadata is not wrong merely because its initial head differs. The core failures are missing public content/links, contradictory URL/status/index signals, script injection, or cross-user leakage—not the absence of Next.js or a generic SEO checklist on every TSX change.

## Primary Sources

- [Google JavaScript SEO](https://developers.google.com/search/docs/crawling-indexing/javascript/javascript-seo-basics), [crawlable links](https://developers.google.com/search/docs/crawling-indexing/links-crawlable).
- [Google noindex](https://developers.google.com/search/docs/crawling-indexing/block-indexing), [robots.txt limits](https://developers.google.com/search/docs/crawling-indexing/robots/intro), [sitemaps](https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap).
- [Next JSON-LD script safety](https://nextjs.org/docs/app/guides/json-ld), [structured-data guidelines](https://developers.google.com/search/docs/appearance/structured-data/sd-policies).
