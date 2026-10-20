import type { Order } from "../../../core/domain/orders/order.ts";
import { OrdersUnavailableError, type OrderRepository } from "../../../core/domain/orders/OrderRepository.ts";
import { sortOrdersForDisplay } from "../../../core/domain/orders/sortOrdersForDisplay.ts";

/** Loaded rows reuse the domain `Order`: it is already plain, render-safe data. */
export type OrdersViewState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "empty" }
  | { kind: "loaded"; orders: Order[] };

export const initialOrdersViewState: OrdersViewState = { kind: "loading" };

export async function loadOrdersViewState(repository: OrderRepository): Promise<OrdersViewState> {
  let orders: Order[];
  try {
    orders = await repository.getOrders();
  } catch (error) {
    if (!(error instanceof OrdersUnavailableError)) throw error;
    return { kind: "error", message: "We couldn't load your orders. Refresh the page to try again." };
  }
  if (orders.length === 0) return { kind: "empty" };
  return { kind: "loaded", orders: sortOrdersForDisplay(orders) };
}
