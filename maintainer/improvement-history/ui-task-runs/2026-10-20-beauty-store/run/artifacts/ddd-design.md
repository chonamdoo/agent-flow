# DDD + architecture design — Store

## Domain model (ddd-architecture)
- Bounded Context: one context, **Storefront** (anonymous shopping over a static catalog). No other context exists in the repo.
- Ubiquitous Language (from `docs/source-prd.md`): Catalog, Category (tree, subtree), Product, Option (sold out when stock 0),
  Price display (original/sale/range), Rail (ranked best-seller rail), Hero slide, Popular search, Recent search,
  Cart, Cart line (product + option), Subtotal, Shipping fee, Estimated total, Free-shipping threshold, Shipping location
  (shippable or not, default currency), Currency preference.
- Entities: Product (id), Category (id), Cart line (key = productId + optionId).
- Value Objects: Money (number shown `$x.xx`), Quantity (1..min(10, stock)), Price display, Listing query (sort, filters, page size, page).
- Aggregate: **Cart** (root) owns its lines; invariants: one line per product+option; quantity in 1..min(10, stock); totals derived, never stored.
  **Preferences** (location + currency); invariant: currency only selectable after a location; choosing a location sets its default currency.
  **Recent searches**; invariants: newest first, no duplicates, at most 30, trimmed non-empty.
- Domain Events (in-memory, not published): ItemAddedToCart (opens added dialog, updates badge), LineRemoved, PreferencesSaved.
- Domain Flow: search/browse → product → choose option + qty → add (merge) → cart (adjust, remove with confirm) → set location → checkout placeholder.
- Every rule above comes from SPEC-1..SPEC-11; nothing invented.

## Selected architecture contract
Mode `stack` → `react-fsd-architecture` (Feature-Sliced Design adapted to the Next.js App Router). Observed layout: empty
app (`app/layout.tsx`, `app/page.tsx` only), no existing slices, no barrels, alias `@/*` → repo root (`tsconfig.json`).
Decision: the Next `app/` folder is the FSD `app` + page composition layer (route files compose widgets); no separate
`pages` layer. No barrels: consumers import stable module paths (`@/features/cart/model/cart`).

## Architecture Boundary Map
Direction `app → widgets → features → entities → shared`; imports only go down or stay inside their slice.

| Layer | Slice / segment | Owns |
|---|---|---|
| shared | `shared/lib/money.ts`, `shared/lib/storage.ts` | `$` formatting; safe `localStorage` JSON read/write (no domain) |
| entities | `entities/catalog/model/catalog.ts`, `entities/catalog/ui/ProductCard.tsx` | fixture access, category tree/subtree/breadcrumb, price display; card UI receiving a product via props |
| features | `features/search/model/search.ts`, `features/search/model/useRecentSearches.ts`, `features/search/ui/SearchBox.tsx` | suggestions (200 ms idle, caps 2/2/10), recent-search policy + persistence, search box + layer |
| features | `features/cart/model/cart.ts`, `features/cart/model/CartProvider.tsx` | Cart aggregate (reducer, merge, clamp, totals, free-shipping) + persisted context |
| features | `features/shipping-prefs/model/PrefsProvider.tsx`, `features/shipping-prefs/ui/PrefsDialog.tsx` | Preferences aggregate + dialog |
| features | `features/listing/model/listing.ts` | sort/filter/paging policy for the category listing |
| widgets | `widgets/header/ui/Header.tsx` | header composing SearchBox + cart link/badge |
| widgets | `widgets/hero-carousel/ui/HeroCarousel.tsx`, `widgets/rail/ui/Rail.tsx` | home regions |
| widgets | `widgets/category-listing/ui/CategoryListing.tsx` | listing screen (uses features/listing) |
| widgets | `widgets/product-detail/ui/ProductDetail.tsx` | product screen (uses features/cart) |
| widgets | `widgets/cart-view/ui/CartView.tsx` | cart screen (uses features/cart + features/shipping-prefs) |
| app | `app/layout.tsx`, `app/providers.tsx`, `app/page.tsx`, `app/category/[categoryId]/page.tsx`, `app/product/[productId]/page.tsx`, `app/cart/page.tsx`, `app/search/page.tsx`, `app/search/SearchQuery.tsx`, `app/checkout/page.tsx` | routing, providers, route-only UI (search query display, checkout placeholder) |

No sideways imports: `widgets/cart-view` composes two features; features never import each other. The badge reads
`features/cart` from the header widget (down).

Server/client: route `page.tsx` files are server components that await `params` and render one client widget;
`'use client'` sits on providers, widgets and feature UI. No server-only code exists (static JSON import), so no
server-only surface is needed.

## Composition Root
`app/layout.tsx` renders `app/providers.tsx` (`'use client'`), which nests `CartProvider` → `PrefsProvider` and renders
`widgets/header` above `children`. Recent searches are owned by `useRecentSearches` inside the search feature (only the
header and search route need them). Providers read `localStorage` in an effect after mount (hydration-safe) and write on change.

## Persistence
`shared/lib/storage.ts` (`readJson(key, fallback, validate)`, `writeJson`) with keys `store.cart`, `store.recent`,
`store.prefs`; invalid JSON or shape → fallback. No cache layer (static data).

## Testability Boundary
- Pure modules (`cart.ts`, `listing.ts`, `search.ts`, `catalog.ts`, `money.ts`) tested directly.
- Widgets tested through Testing Library inside the real providers; `next/navigation` mocked at the module seam
  (`useRouter`, `useSearchParams`); timers via `vi.useFakeTimers()`.
- One test file per SPEC (`tests/spec<n>-<slug>.test.tsx`).

## SOLID
- SRP: each module has one reason to change (cart rules, listing rules, search rules, formatting, storage).
- OCP: new sort options extend a sort table in `listing.ts`; new rails come from fixture data.
- LSP: providers expose a stable hook API; tests substitute storage via jsdom `localStorage`.
- ISP: consumers use narrow hooks (`useCart`, `usePrefs`, `useRecentSearches`) rather than one store.
- DIP: widgets depend on feature hooks/pure functions; features depend on entities/shared, never on widgets or app.

## Slice-plan alignment
The slice plan was written before the FSD contract was supplied (slice-plan received only `Mode: stack`). Its file lists
are updated to these FSD paths; slices, order and verification are unchanged.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: one module per rule set (cart, listing, search, catalog, money, storage)
solid-ocp-extension-points: sort table in listing.ts; rails and hero from fixture data
solid-lsp-contracts: provider hooks keep a stable API over any Storage
solid-isp-consumer-ports: useCart, usePrefs, useRecentSearches narrow hooks
solid-dip-dependency-direction: app → widgets → features → entities → shared, no sideways feature imports
