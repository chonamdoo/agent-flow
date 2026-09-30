import test from 'node:test';
import assert from 'node:assert/strict';
import { createOrdersScreenStore } from '../src/features/orders/ordersScreenStore.ts';

test('empty state shows the product copy from the slice plan', async (t) => {
  t.mock.method(globalThis, 'fetch', async () =>
    new Response(JSON.stringify({ orders: [] }), { status: 200, headers: { 'content-type': 'application/json' } }));
  const store = createOrdersScreenStore();
  await store.load();
  const state = store.getState();
  assert.equal(state.status, 'empty');
  if (state.status !== 'empty') return;
  assert.equal(state.message, 'No orders yet. Your purchases will show up here.');
});
