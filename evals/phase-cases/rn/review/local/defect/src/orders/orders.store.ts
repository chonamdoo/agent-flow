import { formatCents } from '../shared/money.ts';
import type { OrderRow, Order, OrdersState, OrdersStore } from './orders.types.ts';

const EMPTY_ORDERS_MESSAGE = 'No orders yet. Your purchases will show up here.';

const toRow = (order: Order): OrderRow => ({
  id: order.id,
  totalLabel: formatCents(order.totalCents),
  statusLabel: order.status,
});

const newestFirst = (orders: Order[]): Order[] =>
  [...orders].sort((a, b) => b.placedAt.getTime() - a.placedAt.getTime());

export function createOrdersStore(loadOrders: () => Promise<Order[]>): OrdersStore {
  let state: OrdersState = { status: 'loading' };
  let latestRequest = 0;
  const listeners = new Set<() => void>();

  const setState = (next: OrdersState) => {
    state = next;
    listeners.forEach((listener) => listener());
  };

  const loadState = async (): Promise<OrdersState> => {
    let records: Order[];
    try {
      records = await loadOrders();
    } catch {
      return { status: 'empty', message: EMPTY_ORDERS_MESSAGE };
    }
    if (records.length === 0) {
      return { status: 'empty', message: EMPTY_ORDERS_MESSAGE };
    }
    return { status: 'content', rows: newestFirst(records).map(toRow) };
  };

  return {
    getState: (): OrdersState => state,
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
