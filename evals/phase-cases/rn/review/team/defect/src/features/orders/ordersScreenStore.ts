import { formatCents } from '../../shared/money.ts';
import { getJson } from '../../shared/http.ts';

type OrderDto = {
  order_id: string;
  placed_at: string;
  total_cents: number;
  status: string;
};

export type Order = {
  id: string;
  placedAt: Date;
  totalCents: number;
  status: string;
};

async function fetchOrders(): Promise<Order[]> {
  const dtos = parseOrdersResponse(await getJson('/v1/orders'));
  return dtos.map((dto) => ({
    id: dto.order_id,
    placedAt: new Date(dto.placed_at),
    totalCents: dto.total_cents,
    status: dto.status,
  }));
}

function parseOrdersResponse(body: unknown): OrderDto[] {
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

export type OrderRow = {
  id: string;
  totalLabel: string;
  statusLabel: string;
};

export type OrdersScreenState =
  | { status: 'loading' }
  | { status: 'content'; rows: OrderRow[] }
  | { status: 'empty'; message: string }
  | { status: 'error'; message: string };

export type OrdersScreenStore = {
  getState(): OrdersScreenState;
  subscribe(listener: () => void): () => void;
  load(): Promise<void>;
  /** Discards the result of any in-flight load; the screen calls this when it unmounts. */
  cancel(): void;
};

const EMPTY_ORDERS_MESSAGE = 'No orders yet. Your purchases will show up here.';

const toRow = (order: Order): OrderRow => ({
  id: order.id,
  totalLabel: formatCents(order.totalCents),
  statusLabel: order.status,
});

const newestFirst = (orders: Order[]): Order[] =>
  [...orders].sort((a, b) => b.placedAt.getTime() - a.placedAt.getTime());

export function createOrdersScreenStore(loadOrders: () => Promise<Order[]> = fetchOrders): OrdersScreenStore {
  let state: OrdersScreenState = { status: 'loading' };
  let latestRequest = 0;
  const listeners = new Set<() => void>();

  const setState = (next: OrdersScreenState) => {
    state = next;
    listeners.forEach((listener) => listener());
  };

  const loadState = async (): Promise<OrdersScreenState> => {
    const records = await loadOrders();
    if (records.length === 0) {
      return { status: 'empty', message: EMPTY_ORDERS_MESSAGE };
    }
    return { status: 'content', rows: newestFirst(records).map(toRow) };
  };

  return {
    getState: (): OrdersScreenState => state,
    subscribe(listener: () => void): () => void {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    async load(): Promise<void> {
      // A slower earlier load must not overwrite the result of a newer one.
      const request = ++latestRequest;
      setState({ status: 'loading' });
      const next = await loadState();
      if (request === latestRequest) {
        setState(next);
      }
    },
    cancel(): void {
      latestRequest += 1;
    },
  };
}
