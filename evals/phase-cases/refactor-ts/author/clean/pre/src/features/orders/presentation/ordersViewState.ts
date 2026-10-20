import type { Order, OrderState } from "../../../core/domain/orders/order.ts";

export interface OrderRow {
  readonly id: string;
  readonly state: OrderState;
  readonly total: string;
}

export type OrdersViewState = { readonly kind: "empty" } | { readonly kind: "loaded"; readonly rows: readonly OrderRow[] };

export function toOrdersViewState(orders: readonly Order[]): OrdersViewState {
  if (orders.length === 0) {
    return { kind: "empty" };
  }
  const sorted = [...orders].sort((a, b) => b.placedAt.localeCompare(a.placedAt));
  const rows: OrderRow[] = [];
  for (let i = 0; i < sorted.length; i++) {
    const order = sorted[i];
    let total: string;
    if (order.totalCents < 0) {
      const cents = -order.totalCents;
      total = "-US$" + Math.floor(cents / 100) + "." + String(cents % 100).padStart(2, "0");
    } else {
      const cents = order.totalCents;
      total = "US$" + Math.floor(cents / 100) + "." + String(cents % 100).padStart(2, "0");
    }
    rows.push({ id: order.id, state: order.state, total: total });
  }
  return { kind: "loaded", rows: rows };
}
