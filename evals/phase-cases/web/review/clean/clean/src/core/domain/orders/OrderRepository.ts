import type { Order } from "./order.ts";

export interface OrderRepository {
  /** Rejects with `OrdersUnavailableError` when the orders cannot be loaded. */
  getOrders(): Promise<Order[]>;
}

export class OrdersUnavailableError extends Error {
  constructor() {
    super("Orders are unavailable");
    this.name = "OrdersUnavailableError";
  }
}
