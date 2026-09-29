export type OrderShape = {
  id: string;
  placedAt: Date;
  totalCents: number;
  status: string;
};

export type OrderRowShape = {
  id: string;
  totalLabel: string;
  statusLabel: string;
};

export type OrdersStateShape =
  | { status: 'loading' }
  | { status: 'content'; rows: OrderRowShape[] }
  | { status: 'empty'; message: string }
  | { status: 'error'; message: string };

export type OrdersStoreShape = {
  getState(): OrdersStateShape;
  subscribe(listener: () => void): () => void;
  load(): Promise<void>;
  /** Discards the result of any in-flight load; the screen calls this when it unmounts. */
  cancel(): void;
};
