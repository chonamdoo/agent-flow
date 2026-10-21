# DDD Design — Account orders

- Bounded context: Ordering (customer order history).
- Ubiquitous language: order, order state, placed at, total, order history.
- Domain: `Order` is a value object read from the order history; presentation maps it to rows.

## Invariants

- The order history is complete: every order the customer placed appears, in every state. A cancelled order stays in
  the history with state `CANCELLED`; support and refunds rely on the customer seeing it. Presentation never drops
  orders by state.
- Display order is `placedAt` descending.
- A refunded order (state `REFUNDED`) carries a negative `totalCents`: the amount returned. Its row shows the sign
  before the currency, `-US$12.50`; finance support matches refunds by that exact text.
