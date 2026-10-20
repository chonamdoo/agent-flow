import test from "node:test";
import assert from "node:assert/strict";
import { loadOrdersView } from "../../src/features/order-list/model/loadOrdersView.ts";

const response = {
  orders: [
    { order_id: "o-1", state: "DELIVERED", placed_at: "2026-03-01T10:00:00Z", total_cents: 1250 },
    { order_id: "o-2", state: "PENDING", placed_at: "2026-02-01T10:00:00Z", total_cents: 800 },
    { order_id: "o-3", state: "SHIPPED", placed_at: "2026-03-05T10:00:00Z", total_cents: 4300 },
    { order_id: "o-4", state: "CANCELLED", placed_at: "2026-03-09T10:00:00Z", total_cents: 999 },
    { order_id: "o-5", state: "PENDING", placed_at: "2026-03-07T10:00:00Z", total_cents: 150 },
  ],
};

test("orders are listed by status priority, newest first within a status", async () => {
  for (const rows of [response.orders, [...response.orders].reverse()]) {
    const getJson = async (_path: string): Promise<unknown> => ({ orders: rows });
    const state = await loadOrdersView(getJson);
    assert.equal(state.kind, "loaded");
    assert.deepEqual(state.kind === "loaded" ? state.orders.map((order) => order.id) : [], ["o-5", "o-2", "o-3", "o-1", "o-4"]);
  }
});
