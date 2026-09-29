import test from 'node:test';
import assert from 'node:assert/strict';
import { fetchOrders } from '../src/orders/orders.api.ts';

const payload = {
  orders: [
    { order_id: 'o-1', placed_at: '2026-09-01T10:00:00Z', total_cents: 1250, status: 'delivered' },
    { order_id: 'o-2', placed_at: '2026-09-03T08:30:00Z', total_cents: 499, status: 'shipped' },
  ],
};

test('fetchOrders requests /v1/orders and converts the payload', async () => {
  const paths: string[] = [];
  const orders = await fetchOrders(async (path: string) => {
    paths.push(path);
    return payload;
  });
  assert.deepEqual(paths, ['/v1/orders']);
  assert.deepEqual(
    orders.map((order) => [order.id, order.totalCents, order.status, order.placedAt.toISOString()]),
    [
      ['o-1', 1250, 'delivered', '2026-09-01T10:00:00.000Z'],
      ['o-2', 499, 'shipped', '2026-09-03T08:30:00.000Z'],
    ],
  );
});

test('fetchOrders rejects a malformed payload', async () => {
  await assert.rejects(
    fetchOrders(async () => ({ orders: [{ order_id: 'o-1', total_cents: '12.50' }] })),
    TypeError,
  );
});
