# Pause 1 — domain-grill round 1 (controller → user)

**Q1** — Is `docs/source-prd.md` the settled requirement (screens, constants, texts, test ids)? → recommended: yes, implement as written.
**Q2** — Currency: the PRD says money is shown as `$`; does saving a non-USD currency change price display? → recommended: no, currency is a stored preference only; money stays `$` with two decimals.
**Q3** — Checkout: "a placeholder page" — `checkout-button` navigates to `/checkout` showing a placeholder? → recommended: yes.
**Q4** — `/search?q=` shows only the query in `search-query` (no result list)? → recommended: yes, PRD calls it a placeholder.
**Q5** — Persistence keys in `localStorage` (cart, recent searches, location, currency) — key names free? → recommended: yes, implementation detail.

## User answer (scripted)
"`docs/source-prd.md` is the settled requirement; every decision in it is final; do not change its constants, texts or test ids."
Controller reading: Q1 yes; Q2–Q5 accepted as recommended (they follow the PRD text, none changes a constant/text/test id).
