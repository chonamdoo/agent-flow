# DDD Design: Orders list

## Domain Model

- Bounded Context: Ordering (order history read model).
- Ubiquitous Language: order, placed at, total (cents), status.
- Entities: order record (identity `id`); no aggregates change; no domain events.
- Domain Invariants: history is presented newest first by `placedAt`.
- Domain Flow: screen mount → store `load()` → `fetchOrders(getJson)` → rows.

## Architecture Boundary Map

Per `skills/architecture/SKILL.md`: one flat feature folder `src/orders/`.

- `orders.types.ts`: exported record and state types.
- `orders.api.ts`: `fetchOrders(getJson)` converts the wire payload.
- `orders.store.ts`: `createOrdersStore(loadOrders)` state holder, ordering, row labels.
- `orders.screen.tsx`: wires `fetchOrders(getJson)` into the store and renders.

No layer folders, no repository or use-case types; functions are passed instead.

## Composition Root

`orders.screen.tsx` creates the store with `() => fetchOrders(getJson)`.

## Testability Boundary

`fetchOrders` tested with a fake `GetJson`; the store tested with a fake loader function.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: payload conversion (api) and screen state (store) change separately
solid-ocp-extension-points: the store accepts any loader function
solid-lsp-contracts: loaders resolve to order records or reject
solid-isp-consumer-ports: the store depends on a single loader function
solid-dip-dependency-direction: store receives its loader; only the screen wires http
