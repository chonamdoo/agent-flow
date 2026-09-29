# DDD Design: Orders list

## Bounded Context
Ordering (read side). Ubiquitous Language: Order, customer, total (integer cents), placed at.

## Entities / Value Objects
- `Order` (entity, identity `id`); `totalCents` is an integer money amount in minor units.

## Domain Invariants
- Totals are never floating point; the list label formats them.

## Domain Flow
Screen opens → holder `load()` → `OrdersRepository.fetchOrders()` → rows sorted newest first → state.

## Architecture Boundary Map
Selected contract: local `skills/architecture/SKILL.md`.
- Feature folder `lib/orders/` (flat): `orders.dart` (library file, owns imports and `part` directives),
  part files `order.dart`, `orders_repository.dart`, `orders_list_state.dart`, `orders_list_holder.dart`.
- No layer folders and no abstract repository (contract rules 1–2). Records decode on `Order.fromRecord` (rule 4).

## Composition Root
The app shell builds `OrdersRepository(<fetch function>)` and passes it to `OrdersListHolder`.

## Testability Boundary
Tests substitute the fetch function given to `OrdersRepository`; they import only `lib/orders/orders.dart`.

## Completion Gate
architecture-contract: applied
solid-srp-change-reason: decoding, fetching, and screen state are separate part files of one library
solid-ocp-extension-points: a different transport is a different fetch function
solid-lsp-contracts: n/a — no class hierarchy besides the sealed state family
solid-isp-consumer-ports: the holder uses only `fetchOrders`
solid-dip-dependency-direction: the repository depends on an injected function, not a transport class
