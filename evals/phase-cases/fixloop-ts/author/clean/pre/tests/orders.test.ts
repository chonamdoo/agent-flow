import test from "node:test";
import assert from "node:assert/strict";
import { toOrdersViewState } from "../src/features/orders/presentation/ordersViewState.ts";

test("no orders is the empty state", () => {
  assert.deepEqual(toOrdersViewState([]), { kind: "empty" });
});

test("orders are listed newest first with formatted totals", () => {
  const state = toOrdersViewState([
    { id: "o-1", state: "DELIVERED", placedAt: "2026-03-01T10:00:00Z", totalCents: 1250 },
    { id: "o-2", state: "SHIPPED", placedAt: "2026-03-05T10:00:00Z", totalCents: 4300 },
    { id: "o-3", state: "PENDING", placedAt: "2026-02-01T10:00:00Z", totalCents: 805 },
  ]);
  assert.deepEqual(state, {
    kind: "loaded",
    rows: [
      { id: "o-2", state: "SHIPPED", total: "US$43.00" },
      { id: "o-1", state: "DELIVERED", total: "US$12.50" },
      { id: "o-3", state: "PENDING", total: "US$8.05" },
    ],
  });
});
