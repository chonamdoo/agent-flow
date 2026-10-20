import test from "node:test";
import assert from "node:assert/strict";
import { toOrdersViewState } from "../../src/features/orders/presentation/ordersViewState.ts";

// ddd-design.md: a refunded order shows its negative total as -US$x.yy. The visible tests never cover it.
test("a refunded order keeps its negative total", () => {
  const state = toOrdersViewState([
    { id: "o-1", state: "DELIVERED", placedAt: "2026-03-01T10:00:00Z", totalCents: 4300 },
    { id: "r-1", state: "REFUNDED", placedAt: "2026-03-02T10:00:00Z", totalCents: -1250 },
    { id: "r-2", state: "REFUNDED", placedAt: "2026-03-03T10:00:00Z", totalCents: -5 },
  ]);
  assert.deepEqual(state, {
    kind: "loaded",
    rows: [
      { id: "r-2", state: "REFUNDED", total: "-US$0.05" },
      { id: "r-1", state: "REFUNDED", total: "-US$12.50" },
      { id: "o-1", state: "DELIVERED", total: "US$43.00" },
    ],
  });
});
