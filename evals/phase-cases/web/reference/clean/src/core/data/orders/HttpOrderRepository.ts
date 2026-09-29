import type { Order } from "../../domain/orders/order.ts";
import { OrdersUnavailableError, type OrderRepository } from "../../domain/orders/OrderRepository.ts";
import { parseOrdersResponse, toOrder } from "./orderDto.ts";

export type GetJson = (path: string) => Promise<unknown>;

// Simple adapter by design: it owns the one orders request and its DTO mapping, so there is no
// separate remote source or cache.
export class HttpOrderRepository implements OrderRepository {
  readonly #getJson: GetJson;

  constructor(getJson: GetJson) {
    this.#getJson = getJson;
  }

  async getOrders(): Promise<Order[]> {
    try {
      return parseOrdersResponse(await this.#getJson("/api/orders")).map(toOrder);
    } catch {
      throw new OrdersUnavailableError();
    }
  }
}
