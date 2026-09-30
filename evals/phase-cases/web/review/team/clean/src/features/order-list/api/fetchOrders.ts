import { EP_ORDERS } from "../../../shared/api/endpoints.ts";
import type { Order, OrderStatus } from "../model/order.ts";

export type GetJson = (path: string) => Promise<unknown>;

const STATUS_BY_STATE = {
  PENDING: "pending",
  SHIPPED: "shipped",
  DELIVERED: "delivered",
  CANCELLED: "cancelled",
} as const satisfies Record<string, OrderStatus>;

type OrderDto = {
  order_id: string;
  state: keyof typeof STATUS_BY_STATE;
  placed_at: string;
  total_cents: number;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isOrderDto(value: unknown): value is OrderDto {
  return (
    isRecord(value) &&
    typeof value.order_id === "string" &&
    typeof value.state === "string" &&
    Object.hasOwn(STATUS_BY_STATE, value.state) &&
    typeof value.placed_at === "string" &&
    Number.isFinite(Date.parse(value.placed_at)) &&
    Number.isInteger(value.total_cents)
  );
}

export async function fetchOrders(getJson: GetJson): Promise<Order[]> {
  const body = await getJson(EP_ORDERS);
  const rows: unknown = isRecord(body) ? body.orders : undefined;
  if (!Array.isArray(rows) || !rows.every(isOrderDto)) {
    throw new TypeError("Unexpected orders payload");
  }
  return rows.map((dto) => ({
    id: dto.order_id,
    status: STATUS_BY_STATE[dto.state],
    placedAt: dto.placed_at,
    totalCents: dto.total_cents,
  }));
}
