import { fetchOrders, type TGetJson, type TOrder } from "./api.orders.ts";
import { sortOrdersForDisplay } from "./sort.orders.ts";

export type TOrdersView =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "empty" }
  | { kind: "loaded"; orders: TOrder[] };

export const initialOrdersView: TOrdersView = { kind: "loading" };

export async function loadOrdersView(getJson: TGetJson): Promise<TOrdersView> {
  let orders: TOrder[];
  try {
    orders = await fetchOrders(getJson);
  } catch {
    return { kind: "error", message: "We couldn't load your orders. Refresh the page to try again." };
  }
  if (orders.length === 0) return { kind: "empty" };
  return { kind: "loaded", orders: sortOrdersForDisplay(orders) };
}
