# Domain grill — Store (beauty-store PRD)

## Context checked
- Task text: implement the Store in `docs/source-prd.md` (SPEC-1..SPEC-9, test-id contract), data from `fixtures/catalog.json`.
- Repository: no `GLOSSARY.md`, no `GLOSSARY-MAP.md`, no `docs/adr/`. Only `docs/source-prd.md` and the fixture.
- Fixture facts (looked up, not asked): 4 root categories with nested children, 79 products (some with options and per-option stock, stock 0 = sold out), 10 popular searches, 4 hero slides, 2 rails (`best-sellers` ranked), 5 locations (`GLOBAL` not shippable, currency null), 5 currencies, constants block matching the PRD.

## Round 1 (asked)
Q1 PRD settled? Q2 currency affects display? Q3 checkout placeholder route? Q4 search page is query-only placeholder? Q5 storage key names free?

## User round 1 answer
"`docs/source-prd.md` is the settled requirement; every decision in it is final; do not change its constants, texts or test ids."

## Decisions
1. Scope, routes, constants, visible texts and the test-id contract are exactly as in `docs/source-prd.md`.
2. Money is always shown as `$` with two decimals; currency is a saved preference only.
3. `checkout-button` leads to a placeholder `/checkout` page.
4. `/search?q=` shows the submitted query in `search-query`; no result list.
5. localStorage key names are an implementation detail.

## Decision Sources
- Scope, constants, texts, test ids: `docs/source-prd.md`, user round 1
- Money format `$` two decimals; currency is preference only: `docs/source-prd.md`, user round 1
- Checkout placeholder page: `docs/source-prd.md` (Non-goals), user round 1
- Search page is a placeholder showing the query: `docs/source-prd.md` (Scope, SPEC-1), user round 1
- Storage key names free: user round 1

## Glossary / ADR
No glossary or ADR written: the PRD already defines every term (cart line, rail, refine chip, shipping location) and no decision is hard to reverse, so `context_docs_updated: not_needed`.

## Completion Gate
domain-grill: complete
shared_understanding: reached
context_docs_checked: true
context_docs_updated: not_needed
