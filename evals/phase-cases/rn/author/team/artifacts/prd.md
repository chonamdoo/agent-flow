# PRD: Orders list screen

## Goal

Signed-in users can see their orders in the React Native app. The app fetches `GET /v1/orders` through the existing `src/shared/http.ts`, keeps a screen state holder with loading, content, empty and error states, and renders them in an `OrdersScreen` that lets the user retry after an error.

## Scope

- Fetch `GET /v1/orders` (response `{"orders": [{"order_id", "placed_at", "total_cents", "status"}]}`) through `src/shared/http.ts`.
- A screen state holder with loading, content, empty and error states.
- Orders shown newest first with totals formatted by `src/shared/money.ts`.
- An `OrdersScreen` that renders the state and offers retry after an error.

## Non-goals

- Pagination, pull-to-refresh, order detail navigation.

## User Stories

- As a signed-in user, I open the orders screen and see my orders, newest first, with formatted totals.
- As a user with no orders, I see an empty state.
- As a user whose orders fail to load, I see an error and can retry.

## Acceptance Criteria

- Fetch the signed-in user's orders from `GET /v1/orders` through `src/shared/http.ts` and map each entry to an order (id, placed-at date, total cents, status).
- The screen state holder starts in the loading state.
- Orders are shown newest first with totals formatted by `src/shared/money.ts`.
- No orders produce the empty state.
- A failed load produces the error state.
- Subscribers receive every state change and can unsubscribe.

## Edge Cases

- An empty `orders` array produces the empty state.
- A failed request or malformed payload produces the error state, never an empty or stuck loading state.

## Dependencies

- `src/shared/http.ts` (`getJson`) and `src/shared/money.ts` (`formatCents`).

## Risks

- The screen itself has no runtime test; its rendering is checked by type check and review.

## Spec Items

SPEC-1: Fetch the signed-in user's orders from `GET /v1/orders` through `src/shared/http.ts` and map each entry to an order (id, placed-at date, total cents, status).
verify: test:tests/orders-screen-store.test.ts

SPEC-2: The screen state holder starts in the loading state.
verify: test:tests/orders-screen-store.test.ts

SPEC-3: Orders are shown newest first with totals formatted by `src/shared/money.ts`.
verify: test:tests/orders-screen-store.test.ts

SPEC-4: No orders produce the empty state.
verify: test:tests/orders-screen-store.test.ts

SPEC-5: A failed load produces the error state.
verify: test:tests/orders-screen-store.test.ts

SPEC-6: Subscribers receive every state change and can unsubscribe.
verify: test:tests/orders-screen-store.test.ts

## Design Values

## Completion Gate
spec-items: SPEC-1, SPEC-2, SPEC-3, SPEC-4, SPEC-5, SPEC-6
design-values: none
design-values-confirmed: n/a
