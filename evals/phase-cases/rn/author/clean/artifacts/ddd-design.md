# DDD Design: Orders list

## Domain Model

- Bounded Context: Ordering (order history read model).
- Ubiquitous Language: Order, placed at, total (cents), order status, order history.
- Entities: `Order` (identity `id`). Value objects: total in cents, status string.
- Aggregates: none changed; read-only projection.
- Domain Events: none.
- Domain Invariants: history is presented newest first by `placedAt`.
- Domain Flow: screen load → repository port → remote DTO → domain `Order` → UiState rows.

## Architecture Boundary Map

- Domain: `src/core/domain/orders/Order.ts`, `OrderRepository.ts` (port). No data, network, or UI imports.
- Domain error: `OrdersUnavailableError` in `src/core/domain/orders/`.
- Data: `src/core/data/orders/OrderDto.ts` (wire DTO, payload validation, DTO→domain mapper), `OrderRepositoryImpl.ts` implements the port over `GetJson`.
- Feature API: `src/features/orders/api/OrdersEntry.ts` — entry contract (`OrdersEntry` component type, `CreateOrdersEntry`, `OrdersEntryDependencies` holding the domain `OrderRepository` port). No screen internals or data implementations.
- Presentation: `src/features/orders/presentation/` — `OrdersUiState`, row UiModel mapper, `ordersScreenStore.ts` (with `cancel()` for unmount), `OrdersScreen.tsx`, and `createOrdersEntry.tsx` implementing the feature-api contract.

## Dependency Rule

presentation → feature-api → domain ← data; app-shell → feature-api contract, data, and the presentation entry factory. Presentation and feature-api never import data or `src/shared/http.ts`.

## Use Case Boundaries

usecase-interface: n/a
usecase-composition: none
The state holder calls the repository port directly; a single read has no orchestration.

## Repository Boundaries

`OrderRepository.getOrders(): Promise<Order[]>` in domain; `OrderRepositoryImpl` in data.
Recorded simple-adapter choice: one read-only endpoint with no cache, so `OrderRepositoryImpl`
owns the transport call and DTO mapping; no separate remote data source.
Error boundary: transport failures and malformed payloads are translated to the domain
`OrdersUnavailableError`; presentation maps any load failure to the `error` UiState.

## Cache Boundary

cache-required: no
memory-cache: n/a
disk-cache: n/a
cache-invalidation-policy: none; every screen load fetches.

## Mapping Boundary

remote-dto-domain-mapper: required
entity-domain-mapper: n/a
domain-ui-mapper: required

## Composition Root

`src/app/AppShell.tsx` constructs `OrderRepositoryImpl(getJson)` and passes it to `createOrdersEntry`, typed by the feature-api `OrdersEntry` contract. The entry component owns the store instance for its mount lifetime; the screen cancels an in-flight load on unmount.

## Testability Boundary

Repository tested with a fake `GetJson`; store tested with a fake `OrderRepository`.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: DTO mapping, ordering, and UI mapping change for separate reasons and live apart
solid-ocp-extension-points: new sources implement `OrderRepository`
solid-lsp-contracts: any `OrderRepository` returns domain `Order[]` or rejects
solid-isp-consumer-ports: the store needs only `getOrders`
solid-dip-dependency-direction: presentation and data depend on domain; domain depends on nothing
