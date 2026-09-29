# DDD Design: Orders list

## Domain Model

- Bounded Context: Ordering (order history read model).
- Ubiquitous Language: order, placed at, total (cents), status.
- Entities: order record (identity `id`); no aggregates change; no domain events.
- Domain Invariants: history is presented newest first by `placedAt`.
- Domain Flow: screen mount → store `load()` → remote read → rows.

## Architecture Boundary Map

Stack contract `react-native-feature-architecture`: the orders feature owns its code in
`src/features/orders/`; no obligatory domain/data/presentation copies. Team skills under
`skills/` apply to files they match.

- Remote read of `GET /v1/orders` and wire payload type: one feature module.
- `ordersScreenStore.ts`: screen state owner (`createOrdersScreenStore()`), ordering, row labels.
- `OrdersScreen.tsx`: owns the store instance for its lifetime and renders.

## Composition Root

`OrdersScreen.tsx` creates the store; the store defaults to the feature's remote read.

## Testability Boundary

Store tested end to end with `fetch` stubbed; the loader is an optional parameter for finer tests.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: remote read and screen state change separately
solid-ocp-extension-points: optional loader parameter on the store
solid-lsp-contracts: loaders resolve to order records or reject
solid-isp-consumer-ports: the store depends on a single loader function
solid-dip-dependency-direction: store depends on the feature's remote read, which alone uses http
