import { formatCents } from '../shared/money.ts';
import type { OrderRowShape, OrderShape, OrdersStateShape, OrdersStoreShape } from './orders.types.ts';

const EMPTY_ORDERS_MESSAGE = 'No orders yet. Your purchases will show up here.';
const LOAD_ERROR_MESSAGE = 'We could not load your orders. Try again.';

const toRow = (order: OrderShape): OrderRowShape => ({
  id: order.id,
  totalLabel: formatCents(order.totalCents),
  statusLabel: order.status,
});

const newestFirst = (orders: OrderShape[]): OrderShape[] =>
  [...orders].sort((a, b) => b.placedAt.getTime() - a.placedAt.getTime());

export function createOrdersStore(loadOrders: () => Promise<OrderShape[]>): OrdersStoreShape {
  let state: OrdersStateShape = { status: 'loading' };
  let latestRequest = 0;
  const listeners = new Set<() => void>();

  const setState = (next: OrdersStateShape) => {
    state = next;
    listeners.forEach((listener) => listener());
  };

  const loadState = async (): Promise<OrdersStateShape> => {
    let records: OrderShape[];
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
    getState: (): OrdersStateShape => state,
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
