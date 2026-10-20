# DDD Design — Orders list

## Domain Model

- Bounded Context: `Ordering` (read model for the account area).
- Ubiquitous Language: Order, order status (`pending`, `shipped`, `delivered`, `cancelled`),
  placed at, total in cents, orders view.
- Entities: Order (identity `id`). Value Objects: order status, total cents.
- Aggregates: none on the read side; the list is a projection.
- Domain Events: none (read only).
- Domain Invariants: the loaded list contains every order the API returned, each exactly once, in the
  display order recorded in the slice plan.
- Domain Flow: request orders → map payload rows to Order → choose view state → order for display.

## Architecture Boundary Map

| Role | Path | Owns |
|---|---|---|
| core-domain | `src/core/domain/orders/` | `Order`, `OrderStatus`, `OrderRepository` port, display-order policy |
| core-data | `src/core/data/orders/` | `OrderDto`, `toOrder` mapper, `HttpOrderRepository` (implements the port, takes `getJson`) |
| feature-presentation | `src/features/orders/presentation/` | `OrdersViewState`, `initialOrdersViewState`, `loadOrdersViewState(repository)` |

## Dependency Rule

presentation → domain ← data. Domain imports nothing outside `src/core/domain`. Presentation depends on the
`OrderRepository` port only, never on `src/core/data` or DTOs.

## Use Case Boundaries

usecase-interface: n/a
usecase-composition: none
A single read with no orchestration; the presentation loader calls the repository port directly.

## Repository Boundaries

`OrderRepository.getOrders(): Promise<Order[]>` in domain; `HttpOrderRepository` in data adapts
`GET /api/orders` as a recorded simple adapter (owns the request and DTO mapping; no separate remote
source). Transport failures and malformed payloads are translated to the domain
`OrdersUnavailableError`; presentation maps only that error to the `error` view state.

## Cache Boundary

cache-required: no
memory-cache: n/a
disk-cache: n/a
cache-invalidation-policy: n/a — every screen entry reloads.

## Mapping Boundary

remote-dto-domain-mapper: required
entity-domain-mapper: n/a
domain-ui-mapper: optional
DTO→domain in `src/core/data/orders/orderDto.ts`; the view state carries domain `Order` values.

## Composition Root

`app/orders/OrdersClient.tsx` (client boundary under the `app/orders` route) composes
`new HttpOrderRepository(getJson)` from `src/core/network/getJson.ts` once at module scope and passes
the `OrderRepository` port to the presentation hook `useOrdersViewState`, which calls
`loadOrdersViewState`. `OrdersScreen` receives only the view state. No repository instance crosses the
server/client boundary; the server `page.tsx` only renders the client wrapper.

## Testability Boundary

Tests inject a fake `getJson`; no network or React rendering is needed.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: mapping (data), ordering policy (domain), view-state choice (presentation) change for different reasons
solid-ocp-extension-points: new order sources implement OrderRepository without touching presentation
solid-lsp-contracts: any OrderRepository returns Order[] or rejects; the loader treats rejection as error state
solid-isp-consumer-ports: presentation sees only getOrders()
solid-dip-dependency-direction: presentation and data depend on the domain port; domain depends on nothing
