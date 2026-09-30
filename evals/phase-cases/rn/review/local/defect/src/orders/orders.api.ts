import type { GetJson } from '../shared/http.ts';
import type { Order } from './orders.types.ts';

type OrderPayload = {
  order_id: string;
  placed_at: string;
  total_cents: number;
  status: string;
};

export async function fetchOrders(getJson: GetJson): Promise<Order[]> {
  const payloads = parseOrdersResponse(await getJson('/v1/orders'));
  return payloads.map((payload) => ({
    id: payload.order_id,
    placedAt: new Date(payload.placed_at),
    totalCents: payload.total_cents,
    status: payload.status,
  }));
}

function parseOrdersResponse(body: unknown): OrderPayload[] {
  if (!isRecord(body) || !Array.isArray(body.orders)) {
    throw new TypeError('orders response must contain an orders array');
  }
  return body.orders.map(parseOrder);
}

function parseOrder(value: unknown, index: number): OrderPayload {
  if (
    !isRecord(value) ||
    typeof value.order_id !== 'string' ||
    typeof value.placed_at !== 'string' ||
    Number.isNaN(Date.parse(value.placed_at)) ||
    typeof value.total_cents !== 'number' ||
    !Number.isInteger(value.total_cents) ||
    typeof value.status !== 'string'
  ) {
    throw new TypeError(`orders[${index}] is not a valid order`);
  }
  return {
    order_id: value.order_id,
    placed_at: value.placed_at,
    total_cents: value.total_cents,
    status: value.status,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null;
}
