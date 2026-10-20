import { fetchOrders, type GetJson } from "../api/fetchOrders.ts";
import type { Order } from "./order.ts";
import { sortOrdersForDisplay } from "./sortOrdersForDisplay.ts";

export type OrdersView =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "empty" }
  | { kind: "loaded"; orders: Order[] };

export const initialOrdersView: OrdersView = { kind: "loading" };

export async function loadOrdersView(getJson: GetJson): Promise<OrdersView> {
  let orders: Order[];
  try {
    orders = await fetchOrders(getJson);
  } catch {
    return { kind: "error", message: "We couldn't load your orders. Refresh the page to try again." };
  }
  if (orders.length === 0) return { kind: "empty" };
  return { kind: "loaded", orders: sortOrdersForDisplay(orders) };
}
