# DDD Design: Orders list

## Bounded Context
Ordering (read side). Ubiquitous Language: Order, customer, total (integer cents), placed at.

## Entities / Value Objects
- `Order` (entity, identity `id`); `totalCents` is an integer money amount in minor units.

## Domain Invariants
- Totals are never floating point; the list label formats them.

## Domain Flow
Screen opens → notifier `load()` → `OrdersRepository.fetchOrders()` (`GET /orders`) → rows sorted
newest first → state.

## Architecture Boundary Map
No project architecture contract is selected; structure follows the team skill
`team-flutter-conventions`: `lib/features/orders/{order.dart, orders_repository.dart,
orders_list_state.dart, orders_list_notifier.dart}` over the shared `lib/team/api_client.dart`.

## Composition Root
The app shell builds `OrdersRepository(ApiClient(<transport>))` and passes it to `OrdersListNotifier`.

## Testability Boundary
Tests pass a fake transport to `ApiClient`.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: decoding/fetching and screen state change for different reasons
solid-ocp-extension-points: transports are swapped through `ApiClient`
solid-lsp-contracts: n/a — no class hierarchy besides the sealed state family
solid-isp-consumer-ports: the notifier uses only `fetchOrders`
solid-dip-dependency-direction: the repository depends on the injected `ApiClient`
