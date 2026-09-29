export type OrderDto = {
  order_id: string;
  placed_at: string;
  total_cents: number;
  status: string;
};

export function parseOrdersResponse(body: unknown): OrderDto[] {
  if (!isRecord(body) || !Array.isArray(body.orders)) {
    throw new TypeError('orders response must contain an orders array');
  }
  return body.orders.map(parseOrder);
}

function parseOrder(value: unknown, index: number): OrderDto {
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
