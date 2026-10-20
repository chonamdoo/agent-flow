# Red phase — Store

Skills read (required-read plan bodies 1–9): code-generation-discipline, tdd, codebase-design, react-app-shell-error-handling,
react-development-guide, typescript-development-guide, react-fsd-architecture, app-shell-error-contract, write-for-work.
App-shell error handling: no app-wide error host is in scope (static catalog, no network) — n/a for these tests.

Tests written (seams from ddd-design: rendered widgets inside the real providers, pure modules; `next/navigation` mocked):
- tests/spec1-header-search.test.tsx … tests/spec11-persistence.test.tsx (one per SPEC), helpers tests/render.tsx,
  tests/cart-fixture.ts; vitest.config.ts gains the `@` alias, vitest.setup.ts mocks `next/navigation` and clears storage.
- Expected values are literals from `fixtures/catalog.json` and the PRD texts (independent of the code).

Command: `agent-flow run-command -- npm test` (hooks not active in this host session) → exit 1.
Failure summary: 11 failed suites, 0 tests run — every suite fails at import resolution because no production module exists
(`@/widgets/header/ui/Header`, `@/app/providers`, `@/widgets/hero-carousel/ui/HeroCarousel`, `@/entities/catalog/ui/ProductCard`,
`@/widgets/category-listing/ui/CategoryListing`, `@/widgets/product-detail/ui/ProductDetail`, `@/widgets/cart-view/ui/CartView`).
Limitation: the red is a missing-module red, not an assertion red; assertions are first exercised in green.
Note: the workflow asks for all slices' tests in one red phase, which is the horizontal slicing the `tdd` skill names an anti-pattern.

Excerpt:
 FAIL  tests/spec1-header-search.test.tsx — Failed to resolve import "@/widgets/header/ui/Header"
 FAIL  tests/spec11-persistence.test.tsx — Failed to resolve import "@/app/providers"
 FAIL  tests/spec9-shipping-location.test.tsx — Failed to resolve import "@/widgets/cart-view/ui/CartView"
 Test Files  11 failed (11)

## Completion Gate
skills_checked: true
profile-skill-selection: applied
active-profiles: nextjs
changed-file-skill-resolution: applied
required-profile-skills: checked
missing-required-profile-skills: none
project-local-skills: n/a
project-local-skills-used: n/a
regression-test: tests/spec1-header-search.test.tsx, tests/spec2-hero-carousel.test.tsx, tests/spec3-cards-rails.test.tsx, tests/spec4-category-listing.test.tsx, tests/spec5-product-detail.test.tsx, tests/spec6-add-to-cart.test.tsx, tests/spec7-cart-totals.test.tsx, tests/spec8-remove-empty.test.tsx, tests/spec9-shipping-location.test.tsx, tests/spec10-testid-contract.test.tsx, tests/spec11-persistence.test.tsx
red-observed: 1
test-run-evidence: verified
