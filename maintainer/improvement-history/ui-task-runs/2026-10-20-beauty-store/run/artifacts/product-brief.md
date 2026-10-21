# Product brief — Store

- Target user: an anonymous shopper on a cross-border beauty store (desktop browser).
- Painful problem: finding a product (search, category), picking the right option and quantity, and knowing the real cost
  (shipping, free-shipping threshold, whether the store can ship to them) before checkout.
- Current workaround: none in this repository — it is an empty Next.js app; the requirement is `docs/source-prd.md`.
- Why now: the PRD is settled (domain-grill, user round 1) and is scored by a browser test through its test-id contract.
- Narrowest wedge: the five client-side screens and the header over `fixtures/catalog.json`; no backend.
- Value: a complete browse → product → cart loop with live totals and shipping feedback, persisted in `localStorage`.
- Cut list (PRD non-goals): accounts, wishlist, payment, checkout beyond a placeholder, review writing, personalisation,
  analytics, images (coloured blocks), translation, network calls.
- Decision: **build** — all of SPEC-1..SPEC-9 as written; nothing deferred, nothing cut beyond the PRD non-goals.
