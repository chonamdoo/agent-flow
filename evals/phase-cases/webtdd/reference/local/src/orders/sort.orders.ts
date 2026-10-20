import type { TOrder, TOrderStatus } from "./api.orders.ts";

const STATUS_RANK: Record<TOrderStatus, number> = {
  pending: 0,
  shipped: 1,
  delivered: 2,
  cancelled: 3,
};

export function sortOrdersForDisplay(orders: readonly TOrder[]): TOrder[] {
  return [...orders].sort(
    (a, b) =>
      STATUS_RANK[a.status] - STATUS_RANK[b.status] || Date.parse(b.placedAt) - Date.parse(a.placedAt),
  );
}
