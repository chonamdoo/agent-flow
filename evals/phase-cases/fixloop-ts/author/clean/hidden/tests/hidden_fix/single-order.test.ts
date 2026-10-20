import test from "node:test";
import assert from "node:assert/strict";
import { toOrdersViewState } from "../../src/features/orders/presentation/ordersViewState.ts";

test("a single order is listed, not the empty state", () => {
  assert.deepEqual(toOrdersViewState([{ id: "o-9", state: "PAID", placedAt: "2026-03-02T10:00:00Z", totalCents: 999 }]), {
    kind: "loaded",
    rows: [{ id: "o-9", state: "PAID", total: "US$9.99" }],
  });
});
