import test from 'node:test';
import assert from 'node:assert/strict';
import { createOrdersStore } from '../src/orders/orders.store.ts';

const record = (id: string, placedAt: string, totalCents: number) => ({
  id,
  placedAt: new Date(placedAt),
  totalCents,
  status: 'delivered',
});
const returning = (orders: ReturnType<typeof record>[]) => async () => orders;
const failing = () => async (): Promise<ReturnType<typeof record>[]> => {
  throw new Error('network down');
};

test('store starts in the loading state', () => {
  const store = createOrdersStore(returning([]));
  assert.equal(store.getState().status, 'loading');
});

test('load shows orders newest first with formatted totals', async () => {
  const store = createOrdersStore(returning([record('o-1', '2026-09-01T10:00:00Z', 1250), record('o-2', '2026-09-03T08:30:00Z', 499), record('o-3', '2026-09-02T12:00:00Z', 10000)]));
  await store.load();
  const state = store.getState();
  assert.equal(state.status, 'content');
  if (state.status !== 'content') return;
  assert.deepEqual(state.rows.map((row) => row.id), ['o-2', 'o-3', 'o-1']);
  assert.deepEqual(state.rows.map((row) => row.totalLabel), ['$4.99', '$100.00', '$12.50']);
});

test('load with no orders shows the empty state', async () => {
  const store = createOrdersStore(returning([]));
  await store.load();
  assert.equal(store.getState().status, 'empty');
});

test('subscribers receive the latest state and can unsubscribe', async () => {
  const store = createOrdersStore(returning([record('o-1', '2026-09-01T10:00:00Z', 1250)]));
  const seen: string[] = [];
  const unsubscribe = store.subscribe(() => seen.push(store.getState().status));
  await store.load();
  assert.equal(seen.at(-1), 'content');
  unsubscribe();
  const count = seen.length;
  await store.load();
  assert.equal(seen.length, count);
});

test('empty state carries the product copy', async () => {
  const store = createOrdersStore(returning([]));
  await store.load();
  assert.deepEqual(store.getState(), {
    status: 'empty',
    message: 'No orders yet. Your purchases will show up here.',
  });
});

test('a slower earlier load does not overwrite a newer one', async () => {
  const resolvers: Array<(orders: ReturnType<typeof record>[]) => void> = [];
  const store = createOrdersStore(
    () => new Promise<ReturnType<typeof record>[]>((resolve) => resolvers.push(resolve)),
  );
  const first = store.load();
  const second = store.load();
  resolvers[1]([record('o-2', '2026-09-03T08:30:00Z', 499)]);
  await second;
  resolvers[0]([]);
  await first;
  const state = store.getState();
  assert.equal(state.status, 'content');
  if (state.status !== 'content') return;
  assert.deepEqual(state.rows.map((row) => row.id), ['o-2']);
});

test('cancel discards the result of an in-flight load', async () => {
  let resolve: (orders: ReturnType<typeof record>[]) => void = () => {};
  const store = createOrdersStore(
    () =>
      new Promise<ReturnType<typeof record>[]>((settle) => {
        resolve = settle;
      }),
  );
  const pending = store.load();
  const seen: string[] = [];
  store.subscribe(() => seen.push(store.getState().status));
  store.cancel();
  resolve([record('o-1', '2026-09-01T10:00:00Z', 1250)]);
  await pending;
  assert.equal(store.getState().status, 'loading');
  assert.deepEqual(seen, []);
});
