# Main: FSD ownership

Apply only to `apps/main/**`. BO route rules do not define Main placement.

## Layers and placement

The direction is `app → widgets → features → (entities) → shared`. Lower layers cannot import higher layers. Route folders replace canonical FSD pages; entities are conditional, not mandatory scaffolding. App, shared, and i18n have no slices, so sibling-slice restrictions must not treat their segments as slices.

A route-only composition belongs in app. Keep page.tsx server-side and interactive code in a client sibling; route files wire the screen rather than owning HTTP clients, request DTOs, or domain policy. Widgets assemble large page blocks from multiple features. Features own a domain action or domain utility, including behavior reused across routes. Business-independent UI and pure helpers belong in shared.

Slice segments describe purpose: ui for presentation, api for requests/DTOs/mapping, model for schemas/state/domain behavior, lib for internal helpers, and config for settings. A small slice need not pre-create every segment. Avoid ambiguous components/hooks/types as slice segments; this is not a global ban on legitimate shared/types. Top-level src/ui and src/types are not new Main placement destinations.

## Isolation and public surfaces

Different slices in one layer cannot import each other's internals or public entries, including by relative path. Merge them or move genuinely common behavior down. Group directories organize slices without creating a shared escape hatch.

Consumers outside a slice import its public index.ts rather than private files. Every new fixture slice has an explicit entry. In the original policy, existing slices adopted entries when touched rather than through an unrequested bulk migration; that history is not permission to add deep imports here.

Keep server-only exports in index.server.ts and mark the server boundary. Universal/client entries cannot re-export server-only implementation. A client component must not pull a server surface through a barrel. Preserve explicit client ownership rather than adding client directives to whole route trees.

## Shared, entities, and locale

Shared UI must be independent of business meaning and data fetching. Pass data and actions from features/widgets; do not import application API or query clients into shared UI. A shared API module is the single owner of a transport client's common behavior; features must not create duplicate private clients. This fixture demonstrates common response parsing rather than inventing an HTTP refresh policy.

Default to no entity promotion. Promote only when multiple slices need the same domain/DTO mapping invariant; two imports alone are not sufficient. Keep a one-slice model local. An entity cross-public API is justified only when one entity must know another, not as a general isolation bypass. The synthetic product model's actual concepts and consumers require semantic review.

I18n is a separate top-level locale concern, not a business slice or a shared segment. The original internal spelling was .lib, not _lib. Both i18n→shared and shared→i18n are explicitly excluded from lint zones while their direction remains unresolved. Do not silently ban either direction or require a refactor as if a decision had already been made. This exception is distinct from known-debt shared-UI fetching, which remains forbidden for new code.

The follow-up synthetic-fixture policy allows app/widgets/features/entities to consume i18n and forbids i18n from importing those four layers, including their public entries. I18n remains a separate locale concern rather than sharing a layer rank with shared. The i18n↔shared exclusion and the public-entry/server-surface rules remain in force. This follow-up resolves the earlier blanket directional deferral; it is not a reconstruction of the original photo policy.

## Review evidence

Review layer direction, slice isolation, segment placement, public APIs, shared business independence, server/client surfaces, and entity-promotion justification against changed Main files. Emit Main evidence only for Main changes; leave unrelated surface markers inapplicable rather than implying review. The app/shared no-slice cases and i18n/shared exclusions need valid examples alongside deliberate failures. Automated boundary checks do not prove that a product concept deserves an entity.
