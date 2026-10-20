import test from "node:test";
import assert from "node:assert/strict";
import { HttpCartRepository } from "../src/core/data/cart/HttpCartRepository.ts";
import { initialCartViewState, loadCartViewState } from "../src/features/cart/presentation/cartViewState.ts";

const lines = [{ productId: "p-1", quantity: 1 }];

const belowThreshold = {
  currency: "USD",
  lines: [
    { product_id: "p-1", name: "Toner", quantity: 1, unit_price: "28.40", list_price: "35.50",
      line_total: "28.40", discount_rate: 20 },
  ],
  subtotal: "28.40",
  total_discount: "7.10",
  shipping: "25.00",
  free_shipping_threshold: "60.00",
  free_shipping_remaining: "31.60",
  estimated_total: "53.40",
};

const aboveThreshold = {
  ...belowThreshold,
  lines: [{ ...belowThreshold.lines[0], quantity: 3, line_total: "85.20" }],
  subtotal: "85.20",
  total_discount: "21.30",
  shipping: "0.00",
  free_shipping_remaining: "0.00",
  estimated_total: "85.20",
};

function fakePostJson(body: unknown) {
  const calls: Array<{ path: string; body: unknown }> = [];
  const postJson = async (path: string, payload: unknown): Promise<unknown> => {
    calls.push({ path, body: payload });
    return body;
  };
  return { calls, postJson };
}

test("the quote is requested from POST /api/cart/quote and money strings become integer cents", async () => {
  const { calls, postJson } = fakePostJson(belowThreshold);
  const quote = await new HttpCartRepository(postJson).quote(lines);
  assert.deepEqual(calls, [{ path: "/api/cart/quote", body: { lines: [{ product_id: "p-1", quantity: 1 }] } }]);
  assert.equal(quote.subtotalCents, 2840);
  assert.equal(quote.shippingCents, 2500);
  assert.equal(quote.freeShippingRemainingCents, 3160);
  assert.equal(quote.estimatedTotalCents, 5340);
  assert.equal(quote.lines[0].lineTotalCents, 2840);
  assert.equal(quote.lines[0].listPriceCents, 3550);
});

test("the cart view starts as loading", () => {
  assert.equal(initialCartViewState.kind, "loading");
});

test("an empty cart is empty without asking the API", async () => {
  const { calls, postJson } = fakePostJson(belowThreshold);
  const state = await loadCartViewState(new HttpCartRepository(postJson), []);
  assert.equal(state.kind, "empty");
  assert.equal(calls.length, 0);
});

test("a failed quote is the error state", async () => {
  const postJson = async (): Promise<unknown> => {
    throw new Error("503 Service Unavailable");
  };
  const state = await loadCartViewState(new HttpCartRepository(postJson), lines);
  assert.equal(state.kind, "error");
});

test("a loaded cart shows US$ prices and how much is left for free shipping", async () => {
  const { postJson } = fakePostJson(belowThreshold);
  const state = await loadCartViewState(new HttpCartRepository(postJson), lines);
  assert.equal(state.kind, "loaded");
  if (state.kind !== "loaded") return;
  assert.equal(state.lines[0].unitPrice, "US$28.40");
  assert.equal(state.lines[0].lineTotal, "US$28.40");
  assert.equal(state.lines[0].discountLabel, "20%");
  assert.equal(state.subtotal, "US$28.40");
  assert.equal(state.shipping, "US$25.00");
  assert.equal(state.shippingMessage, "Only US$31.60 left for free shipping");
  assert.equal(state.estimatedTotal, "US$53.40");
});

test("a cart over the threshold ships free", async () => {
  const { postJson } = fakePostJson(aboveThreshold);
  const state = await loadCartViewState(new HttpCartRepository(postJson), [{ productId: "p-1", quantity: 3 }]);
  assert.equal(state.kind, "loaded");
  if (state.kind !== "loaded") return;
  assert.equal(state.shipping, "Free");
  assert.equal(state.shippingMessage, "Free shipping");
  assert.equal(state.estimatedTotal, "US$85.20");
});
