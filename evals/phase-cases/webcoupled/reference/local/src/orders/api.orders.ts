export type TOrderStatus = "pending" | "shipped" | "delivered" | "cancelled";

export type TOrder = {
  id: string;
  status: TOrderStatus;
  placedAt: string;
  totalCents: number;
};

export type TGetJson = (path: string) => Promise<unknown>;

const STATUS_BY_STATE = {
  PENDING: "pending",
  SHIPPED: "shipped",
  DELIVERED: "delivered",
  CANCELLED: "cancelled",
} as const satisfies Record<string, TOrderStatus>;

type TOrderPayload = {
  order_id: string;
  state: keyof typeof STATUS_BY_STATE;
  placed_at: string;
  total_cents: number;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isOrderPayload(value: unknown): value is TOrderPayload {
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

export async function fetchOrders(getJson: TGetJson): Promise<TOrder[]> {
  const body = await getJson("/api/orders");
  const rows: unknown = isRecord(body) ? body.orders : undefined;
  if (!Array.isArray(rows) || !rows.every(isOrderPayload)) {
    throw new TypeError("Unexpected orders payload");
  }
  return rows.map((payload) => ({
    id: payload.order_id,
    status: STATUS_BY_STATE[payload.state],
    placedAt: payload.placed_at,
    totalCents: payload.total_cents,
  }));
}
