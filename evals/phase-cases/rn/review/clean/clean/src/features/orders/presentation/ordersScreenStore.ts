import { sortNewestFirst, type Order } from '../../../core/domain/orders/Order.ts';
import type { OrderRepository } from '../../../core/domain/orders/OrderRepository.ts';
import { toOrderRow, type OrdersUiState } from './OrdersUiState.ts';

const EMPTY_ORDERS_MESSAGE = 'No orders yet. Your purchases will show up here.';
const LOAD_ERROR_MESSAGE = 'We could not load your orders. Try again.';

export type OrdersScreenStore = {
  getState(): OrdersUiState;
  subscribe(listener: () => void): () => void;
  load(): Promise<void>;
  /** Discards the result of any in-flight load; the screen calls this when it unmounts. */
  cancel(): void;
};

export function createOrdersScreenStore(repository: OrderRepository): OrdersScreenStore {
  let state: OrdersUiState = { status: 'loading' };
  let latestRequest = 0;
  const listeners = new Set<() => void>();

  const setState = (next: OrdersUiState) => {
    state = next;
    listeners.forEach((listener) => listener());
  };

  const loadState = async (): Promise<OrdersUiState> => {
    let records: Order[];
    try {
      records = await repository.getOrders();
    } catch {
      return { status: 'error', message: LOAD_ERROR_MESSAGE };
    }
    if (records.length === 0) {
      return { status: 'empty', message: EMPTY_ORDERS_MESSAGE };
    }
    return { status: 'content', rows: sortNewestFirst(records).map(toOrderRow) };
  };

  return {
    getState: (): OrdersUiState => state,
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
