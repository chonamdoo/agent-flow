# Slice Plan — Account orders list

Task: the account orders list view state.

Architecture contract: Clean Architecture (`--architecture-mode clean`, profile nextjs).

## Slice 1 — Orders view state

- Files: src/core/domain/orders/order.ts, src/features/orders/presentation/ordersViewState.ts
- Scope: the `Order` domain type and `toOrdersViewState(orders)`, which returns `{kind: "empty"}` for no orders and
  otherwise `{kind: "loaded", rows}` newest first with formatted totals.
- Boundary: core-domain `src/core/domain/orders/`; feature-presentation `src/features/orders/presentation/` depends on
  the domain type only.
- Verification: `node --test "tests/*.test.ts"`.

## Out of Scope

- Data access, components, routing.
