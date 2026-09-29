import test from "node:test";
import assert from "node:assert/strict";
import { HttpOrderRepository } from "../src/core/data/orders/HttpOrderRepository.ts";
import { initialOrdersViewState, loadOrdersViewState } from "../src/features/orders/presentation/ordersViewState.ts";
import { OrdersUnavailableError } from "../src/core/domain/orders/OrderRepository.ts";

const response = {
  orders: [
    { order_id: "o-1", state: "DELIVERED", placed_at: "2026-03-01T10:00:00Z", total_cents: 1250 },
    { order_id: "o-2", state: "PENDING", placed_at: "2026-02-01T10:00:00Z", total_cents: 800 },
    { order_id: "o-3", state: "SHIPPED", placed_at: "2026-03-05T10:00:00Z", total_cents: 4300 },
    { order_id: "o-4", state: "CANCELLED", placed_at: "2026-03-09T10:00:00Z", total_cents: 999 },
    { order_id: "o-5", state: "PENDING", placed_at: "2026-03-07T10:00:00Z", total_cents: 150 },
  ],
};

function fakeGetJson(body: unknown) {
  const calls: string[] = [];
  const getJson = async (path: string): Promise<unknown> => {
    calls.push(path);
    return body;
  };
  return { calls, getJson };
}

const failingGetJson = async (_path: string): Promise<unknown> => {
  throw new Error("503 Service Unavailable");
};

test("orders are requested from GET /api/orders and mapped from the payload", async () => {
  const { calls, getJson } = fakeGetJson(response);
  const orders = await new HttpOrderRepository(getJson).getOrders();
  assert.deepEqual(calls, ["/api/orders"]);
  assert.equal(orders.length, 5);
  assert.deepEqual(orders.find((order) => order.id === "o-3"), { id: "o-3", status: "shipped", placedAt: "2026-03-05T10:00:00Z", totalCents: 4300 });
});

test("the orders screen starts in the loading state", () => {
  assert.deepEqual(initialOrdersViewState, { kind: "loading" });
});

test("loaded state carries every order", async () => {
  const state = await loadOrdersViewState(new HttpOrderRepository(fakeGetJson(response).getJson));
  assert.equal(state.kind, "loaded");
  const ids = state.kind === "loaded" ? state.orders.map((order) => order.id).sort() : [];
  assert.deepEqual(ids, ["o-1", "o-2", "o-3", "o-4", "o-5"]);
});

test("no orders yields the empty state", async () => {
  const state = await loadOrdersViewState(new HttpOrderRepository(fakeGetJson({ orders: [] }).getJson));
  assert.deepEqual(state, { kind: "empty" });
});

test("a failed request yields the error state", async () => {
  const state = await loadOrdersViewState(new HttpOrderRepository(failingGetJson));
  assert.equal(state.kind, "error");
});

test("orders are listed by status priority, newest first within a status", async () => {
  for (const rows of [response.orders, [...response.orders].reverse()]) {
    const state = await loadOrdersViewState(new HttpOrderRepository(fakeGetJson({ orders: rows }).getJson));
    assert.equal(state.kind, "loaded");
    assert.deepEqual(state.kind === "loaded" ? state.orders.map((order) => order.id) : [], ["o-5", "o-2", "o-3", "o-1", "o-4"]);
  }
});

test("a payload with an unknown order state yields the error state", async () => {
  const payload = { orders: [{ order_id: "o-9", state: "LOST", placed_at: "2026-03-01T10:00:00Z", total_cents: 100 }] };
  const state = await loadOrdersViewState(new HttpOrderRepository(fakeGetJson(payload).getJson));
  assert.equal(state.kind, "error");
});

test("repository failures surface as OrdersUnavailableError", async () => {
  await assert.rejects(new HttpOrderRepository(failingGetJson).getOrders(), OrdersUnavailableError);
});
