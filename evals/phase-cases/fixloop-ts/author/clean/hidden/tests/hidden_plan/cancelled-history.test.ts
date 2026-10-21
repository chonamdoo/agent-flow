import test from "node:test";
import assert from "node:assert/strict";
import { toOrdersViewState } from "../../src/features/orders/presentation/ordersViewState.ts";

test("cancelled orders stay in the history in date order", () => {
  const state = toOrdersViewState([
    { id: "o-1", state: "DELIVERED", placedAt: "2026-03-01T10:00:00Z", totalCents: 1250 },
    { id: "o-4", state: "CANCELLED", placedAt: "2026-03-09T10:00:00Z", totalCents: 999 },
  ]);
  assert.deepEqual(state, {
    kind: "loaded",
    rows: [
      { id: "o-4", state: "CANCELLED", total: "US$9.99" },
      { id: "o-1", state: "DELIVERED", total: "US$12.50" },
    ],
  });
});
