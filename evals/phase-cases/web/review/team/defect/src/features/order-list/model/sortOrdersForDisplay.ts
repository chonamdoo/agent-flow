import type { Order, OrderStatus } from "./order.ts";

const STATUS_RANK: Record<OrderStatus, number> = {
  pending: 0,
  shipped: 1,
  delivered: 2,
  cancelled: 3,
};

export function sortOrdersForDisplay(orders: readonly Order[]): Order[] {
  return [...orders].sort(
    (a, b) =>
      STATUS_RANK[a.status] - STATUS_RANK[b.status] || new Date(b.placedAt).getDate() - new Date(a.placedAt).getDate(),
  );
}
