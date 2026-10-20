# Slice plan — Store

Architecture contract: `stack` mode (Next.js App Router). Boundaries in its terms: **route** (`app/**/page.tsx`, server
shell that resolves params and renders one client screen), **screen component** (`components/*`, client, owns UI state),
**domain module** (`lib/*`, pure TypeScript, no React, no DOM except `lib/storage.ts`), **store provider** (React context
holding cart, recent searches and preferences, persisted through `lib/storage.ts`). Data source: `fixtures/catalog.json`
imported statically (no network). DDD not selected.

Tests: Vitest + Testing Library, one file per Spec Item, run as `npm test -- <file-stem>`.

## Slice 1 — Catalog, money, storage and store provider (SPEC-11; base for all)
- Boundary: domain modules + store provider + root layout.
- Files: lib/catalog.ts, lib/money.ts, lib/storage.ts, lib/cart.ts, components/StoreProvider.tsx, app/layout.tsx, app/globals.css, tests/spec11-persistence.test.tsx
- Verification: `npm test -- spec11-persistence` (cart, recent searches, location, currency survive a remount via `localStorage`; invalid stored JSON falls back to defaults; `formatMoney` gives `$` + two decimals).

## Slice 2 — Header search and search page (SPEC-1)
- Boundary: header screen component (rendered by the layout) + `/search` route.
- Files: components/Header.tsx, components/SearchQuery.tsx, app/search/page.tsx, tests/spec1-header-search.test.tsx
- Verification: `npm test -- spec1-header-search` (layer on focus, 10 popular terms in order, recent hidden when empty, 200 ms idle suggestions with caps 2/2/10, Escape, Enter navigation, recent first/no dup/max 30, whitespace no-op).

## Slice 3 — Home: hero carousel, rails, product cards (SPEC-2, SPEC-3)
- Boundary: home route + carousel/rail/card screen components.
- Files: app/page.tsx, components/HeroCarousel.tsx, components/Rail.tsx, components/ProductCard.tsx, tests/spec2-hero-carousel.test.tsx, tests/spec3-cards-rails.test.tsx
- Verification: `npm test -- spec2-hero-carousel` (6 s auto-advance with fake timers, "n / total", toggle, wrap) and `npm test -- spec3-cards-rails` (price display variants, rank 1..N, rail prev/next disabled at ends).

## Slice 4 — Category listing (SPEC-4)
- Boundary: `/category/[categoryId]` route + listing screen component; sort/filter/paging logic in `lib/catalog.ts`.
- Files: app/category/[categoryId]/page.tsx, components/CategoryListing.tsx, lib/catalog.ts, tests/spec4-category-listing.test.tsx
- Verification: `npm test -- spec4-category-listing` (title, "N items" subtree count, sort options/default, 24 first, union filters, chips, clear-all, MORE (k/P), hide on last page, sort/page-size reset to page 1, empty-result text).

## Slice 5 — Product detail and add to cart (SPEC-5, SPEC-6)
- Boundary: `/product/[productId]` route + product screen component + cart reducer.
- Files: app/product/[productId]/page.tsx, components/ProductDetail.tsx, lib/cart.ts, components/Header.tsx, tests/spec5-product-detail.test.tsx, tests/spec6-add-to-cart.test.tsx
- Verification: `npm test -- spec5-product-detail` (breadcrumb path, disabled steppers before choice, option list, Sold out disabled, label with price × qty, bounds min(10, stock)/1) and `npm test -- spec6-add-to-cart` (dialog, badge = distinct lines, merge on re-add).

## Slice 6 — Cart, removal, shipping location, checkout placeholder (SPEC-7, SPEC-8, SPEC-9)
- Boundary: `/cart` and `/checkout` routes + cart screen and preferences dialog components; totals in `lib/cart.ts`.
- Files: app/cart/page.tsx, components/CartView.tsx, components/PrefsDialog.tsx, app/checkout/page.tsx, lib/cart.ts, tests/spec7-cart-totals.test.tsx, tests/spec8-remove-empty.test.tsx, tests/spec9-shipping-location.test.tsx
- Verification: `npm test -- spec7-cart-totals`, `npm test -- spec8-remove-empty`, `npm test -- spec9-shipping-location`.

## Slice 7 — Test-id contract sweep (SPEC-10)
- Boundary: test only, across every screen.
- Files: tests/spec10-testid-contract.test.tsx
- Verification: `npm test -- spec10-testid-contract` (each screen renders the contract ids; singletons occur once; native selects; checkboxes; `aria-valuenow`).

## Whole-run verification
`npm run lint`, `npm test`, `npm run build`; then the browser scorer outside the workflow.
