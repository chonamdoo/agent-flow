import type { Order } from './Order.ts';

export interface OrderRepository {
  /** Rejects with `OrdersUnavailableError` when the orders cannot be read. */
  getOrders(): Promise<Order[]>;
}
