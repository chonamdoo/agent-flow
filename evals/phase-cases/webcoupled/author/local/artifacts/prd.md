# PRD: Orders list

## Goal

Signed-in users can see their orders in the account area of the Next.js app. The app loads `GET /api/orders` through an injected `getJson(path)` transport and exposes the view state the orders screen renders — loading, error, empty and loaded — with the orders sorted for display.

## Scope

- Load `GET /api/orders` (response `{"orders": [{"order_id", "state", "placed_at", "total_cents"}]}`) through an injected `getJson(path)`.
- View state for the orders screen: loading, error, empty and loaded.
- Loaded orders are sorted for display.

## Non-goals

- Rendering components, pagination, order detail, currency formatting.

## User Stories

- As a signed-in user, I open the account orders screen and see all my orders.
- As a user with no orders, I see an empty state.
- As a user whose orders fail to load, I see an error state.

## Acceptance Criteria

- Load the signed-in user's orders from `GET /api/orders` through the injected `getJson(path)` transport and map each payload row to an order.
- The orders view state starts as loading.
- The loaded view state carries every order.
- No orders produce the empty view state.
- A failed request produces the error view state.

## Edge Cases

- An empty `orders` array produces the empty state.
- A failed request produces the error state.

## Dependencies

- An injected `getJson(path)` transport.

## Risks

- The API returns orders in no guaranteed order.

## Spec Items

SPEC-1: Load the signed-in user's orders from `GET /api/orders` through the injected `getJson(path)` transport and map each payload row to an order.
verify: test:tests/orders.test.ts

SPEC-2: The orders view state starts as loading.
verify: test:tests/orders.test.ts

SPEC-3: The loaded view state carries every order.
verify: test:tests/orders.test.ts

SPEC-4: No orders produce the empty view state.
verify: test:tests/orders.test.ts

SPEC-5: A failed request produces the error view state.
verify: test:tests/orders.test.ts

## Design Values

## Completion Gate
spec-items: SPEC-1, SPEC-2, SPEC-3, SPEC-4, SPEC-5
design-values: none
design-values-confirmed: n/a
