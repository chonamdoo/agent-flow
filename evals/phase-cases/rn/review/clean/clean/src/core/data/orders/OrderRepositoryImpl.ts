import type { GetJson } from '../../../shared/http.ts';
import type { Order } from '../../domain/orders/Order.ts';
import type { OrderRepository } from '../../domain/orders/OrderRepository.ts';
import { OrdersUnavailableError } from '../../domain/orders/OrdersUnavailableError.ts';
import { parseOrdersResponse, toOrder, type OrderDto } from './OrderDto.ts';

/**
 * Simple adapter: one read-only endpoint with no cache, so this class owns the
 * transport call and DTO mapping instead of delegating to a separate remote source.
 */
export class OrderRepositoryImpl implements OrderRepository {
  private readonly getJson: GetJson;

  constructor(getJson: GetJson) {
    this.getJson = getJson;
  }

  async getOrders(): Promise<Order[]> {
    let dtos: OrderDto[];
    try {
      dtos = parseOrdersResponse(await this.getJson('/v1/orders'));
    } catch {
      throw new OrdersUnavailableError();
    }
    return dtos.map(toOrder);
  }
}
