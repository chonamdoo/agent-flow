import test from 'node:test';
import assert from 'node:assert/strict';
import { createOrdersScreenStore } from '../src/features/orders/ordersScreenStore.ts';

type TestContext = { mock: { method: (target: object, name: string, impl: unknown) => unknown } };

const wire = (id: string, placedAt: string, totalCents: number) => ({
  order_id: id,
  placed_at: placedAt,
  total_cents: totalCents,
  status: 'delivered',
});

function stubFetch(t: TestContext, respond: () => Response): string[] {
  const urls: string[] = [];
  t.mock.method(globalThis, 'fetch', async (url: unknown) => {
    urls.push(String(url));
    return respond();
  });
  return urls;
}

const ok = (orders: unknown[]) => () =>
  new Response(JSON.stringify({ orders }), { status: 200, headers: { 'content-type': 'application/json' } });

test('store starts in the loading state without fetching', (t) => {
  const urls = stubFetch(t, ok([]));
  const store = createOrdersScreenStore();
  assert.equal(store.getState().status, 'loading');
  assert.deepEqual(urls, []);
});

test('load fetches /v1/orders and shows orders newest first with formatted totals', async (t) => {
  const urls = stubFetch(t, ok([
    wire('o-1', '2026-09-01T10:00:00Z', 1250),
    wire('o-2', '2026-09-03T08:30:00Z', 499),
    wire('o-3', '2026-09-02T12:00:00Z', 10000),
  ]));
  const store = createOrdersScreenStore();
  await store.load();
  assert.deepEqual(urls, ['https://api.example.com/v1/orders']);
  const state = store.getState();
  assert.equal(state.status, 'content');
  if (state.status !== 'content') return;
  assert.deepEqual(state.rows.map((row) => row.id), ['o-2', 'o-3', 'o-1']);
  assert.deepEqual(state.rows.map((row) => row.totalLabel), ['$4.99', '$100.00', '$12.50']);
});

test('load with no orders shows the empty state', async (t) => {
  stubFetch(t, ok([]));
  const store = createOrdersScreenStore();
  await store.load();
  assert.equal(store.getState().status, 'empty');
});

test('load failure shows the error state', async (t) => {
  stubFetch(t, () => new Response('', { status: 503 }));
  const store = createOrdersScreenStore();
  await store.load();
  const state = store.getState();
  assert.equal(state.status, 'error');
  if (state.status !== 'error') return;
  assert.equal(typeof state.message, 'string');
  assert.ok(state.message.length > 0);
});

test('subscribers receive the latest state and can unsubscribe', async (t) => {
  stubFetch(t, ok([wire('o-1', '2026-09-01T10:00:00Z', 1250)]));
  const store = createOrdersScreenStore();
  const seen: string[] = [];
  const unsubscribe = store.subscribe(() => seen.push(store.getState().status));
  await store.load();
  assert.equal(seen.at(-1), 'content');
  unsubscribe();
  const count = seen.length;
  await store.load();
  assert.equal(seen.length, count);
});
