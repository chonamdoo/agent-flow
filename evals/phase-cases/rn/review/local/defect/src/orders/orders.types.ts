export type Order = {
  id: string;
  placedAt: Date;
  totalCents: number;
  status: string;
};

export type OrderRow = {
  id: string;
  totalLabel: string;
  statusLabel: string;
};

export type OrdersState =
  | { status: 'loading' }
  | { status: 'content'; rows: OrderRow[] }
  | { status: 'empty'; message: string }
  | { status: 'error'; message: string };

export type OrdersStore = {
  getState(): OrdersState;
  subscribe(listener: () => void): () => void;
  load(): Promise<void>;
  /** Discards the result of any in-flight load; the screen calls this when it unmounts. */
  cancel(): void;
};
