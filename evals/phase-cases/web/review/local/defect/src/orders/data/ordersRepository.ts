import { fetchOrders, type TGetJson, type TOrder } from "../api.orders.ts";

export interface OrdersRepository {
  list(): Promise<TOrder[]>;
}

export class HttpOrdersRepository implements OrdersRepository {
  readonly #getJson: TGetJson;

  constructor(getJson: TGetJson) {
    this.#getJson = getJson;
  }

  list(): Promise<TOrder[]> {
    return fetchOrders(this.#getJson);
  }
}
