import type { Order } from "../../../core/domain/orders/order.ts";
import type { OrderRepository } from "../../../core/domain/orders/OrderRepository.ts";

export class OrdersService {
  readonly #repository: OrderRepository;

  constructor(repository: OrderRepository) {
    this.#repository = repository;
  }

  getOrders(): Promise<Order[]> {
    return this.#repository.getOrders();
  }
}
