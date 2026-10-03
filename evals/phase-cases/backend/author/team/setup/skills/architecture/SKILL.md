---
name: architecture
description: Approved local architecture for the ledger service. Use when implementing Python records, listing rules, HTTP handlers, or wiring under src/.
---

# Ledger service boundaries

The project selects this local contract for the orders implementation. It extends
the existing inventory pattern; these ownership and wiring decisions are approved
before the green phase, rather than left to an author in pending mode.

- `src/domain/<context>/models.py` owns context records and vocabulary.
- `src/domain/<context>/listing.py` owns pure listing rules. It receives typed rows,
  scopes them to the customer, filters and orders them; it has no storage or HTTP dependency.
- `src/app/<context>_http.py` owns query validation and HTTP response mapping using
  `app.responses`. It receives the row loader through composition.
- `src/app/container.py` owns wiring. It converts raw stored rows to typed records
  once and supplies a zero-argument loader, following `build_stock_level_handler`.
  A database can supply the same row-loader contract without changing listing rules.
- Apply `skills/orders-team-conventions/SKILL.md` to record representations.
  The selected contract adds no repository interface, Clean role map, or storage implementation.
