# DDD Design: Orders list

## Bounded Context
Ordering (read side). Ubiquitous Language: Order, customer, total (integer cents), placed at.

## Entities / Value Objects
- `Order` (entity, identity `id`); `totalCents` is an integer money amount in minor units.

## Domain Invariants
- Totals are never floating point; display formatting happens in presentation.

## Domain Flow
Screen opens → holder `load()` → `OrderRepository.fetchOrders()` → rows sorted newest first → state.

## Architecture Boundary Map
- core-domain `lib/core/domain/orders/`: `Order`, `OrderRepository` (port), `OrdersUnavailableException`. Imports only domain files.
- core-data `lib/core/data/orders/`: `OrdersRemoteSource` (port), `HttpOrdersRemoteSource` (`dart:io` adapter), `OrderDto`, `OrderRepositoryImpl`.
- feature-api `lib/features/orders/api/`: `ordersListRoute` (`/orders`), the feature's entry contract paired with its presentation.
- feature-presentation `lib/features/orders/presentation/`: `OrdersListState`, `OrderRow`, `OrdersListHolder`.
- app-shell `lib/app/`: `createOrdersListHolder` composition function.

## Dependency Rule
presentation → domain ← data. Presentation never imports data; domain imports nothing outside domain.

## Use Case Boundaries
usecase-interface: n/a
usecase-composition: none
A single read with no orchestration; the holder calls the repository port directly.

## Repository Boundaries
Port `OrderRepository` in domain; adapter `OrderRepositoryImpl` in data over `OrdersRemoteSource`.
Raw transport (`HttpException`) and decoding (`FormatException`) failures stay in data; the repository
translates them into the domain `OrdersUnavailableException`, which the holder maps to `OrdersError`.

## Cache Boundary
cache-required: no
memory-cache: n/a
disk-cache: n/a
cache-invalidation-policy: none — every `load()` fetches.

## Mapping Boundary
remote-dto-domain-mapper: required
entity-domain-mapper: n/a
domain-ui-mapper: required

## Composition Root
`lib/app/orders_composition.dart` (`createOrdersListHolder`) constructs
`OrderRepositoryImpl(HttpOrdersRemoteSource(httpClient, ordersUri))` and passes it to `OrdersListHolder`.

## Testability Boundary
Tests fake `OrderRepository` for the holder and `OrdersRemoteSource` for the repository adapter.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: transport, mapping, and screen state change for different reasons and live apart
solid-ocp-extension-points: new sources implement `OrdersRemoteSource`
solid-lsp-contracts: any `OrderRepository` returns domain orders or throws
solid-isp-consumer-ports: holder depends only on `fetchOrders`
solid-dip-dependency-direction: presentation and data depend on the domain port
