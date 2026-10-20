import type { Order, OrderState } from "../../../core/domain/orders/order.ts";

export interface OrderRow {
  readonly id: string;
  readonly state: OrderState;
  readonly total: string;
}

export type OrdersViewState = { readonly kind: "empty" } | { readonly kind: "loaded"; readonly rows: readonly OrderRow[] };

const formatTotal = (cents: number): string => {
  const sign = cents < 0 ? "-" : "";
  const amount = Math.abs(cents);
  return `${sign}US$${Math.floor(amount / 100)}.${String(amount % 100).padStart(2, "0")}`;
};

const toOrderRow = (order: Order): OrderRow => ({ id: order.id, state: order.state, total: formatTotal(order.totalCents) });

export function toOrdersViewState(orders: readonly Order[]): OrdersViewState {
  if (orders.length === 0) {
    return { kind: "empty" };
  }
  const rows = [...orders].sort((a, b) => b.placedAt.localeCompare(a.placedAt)).map(toOrderRow);
  return { kind: "loaded", rows };
}
