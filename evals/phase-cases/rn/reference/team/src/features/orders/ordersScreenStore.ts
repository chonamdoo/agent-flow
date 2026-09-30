import { formatCents } from '../../shared/money.ts';
import { fetchOrders, type Order } from './orders.remote.ts';

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
const LOAD_ERROR_MESSAGE = 'We could not load your orders. Try again.';

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
    let records: Order[];
    try {
      records = await loadOrders();
    } catch {
      return { status: 'error', message: LOAD_ERROR_MESSAGE };
    }
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
