import test from "node:test";
import assert from "node:assert/strict";
import { HttpCartRepository } from "../../src/core/data/cart/HttpCartRepository.ts";
import { loadCartViewState } from "../../src/features/cart/presentation/cartViewState.ts";

const quote = {
  currency: "USD",
  lines: [
    { product_id: "p-1", name: "Toner", quantity: 2, unit_price: "28.83", list_price: "35.50",
      line_total: "57.66", discount_rate: 18 },
    { product_id: "p-2", name: "Serum", quantity: 1, unit_price: "15.00", list_price: "15.00",
      line_total: "15.00", discount_rate: 0 },
  ],
  subtotal: "72.66",
  total_discount: "13.34",
  shipping: "0.00",
  free_shipping_threshold: "60.00",
  free_shipping_remaining: "0.00",
  estimated_total: "72.66",
};

async function load(body: unknown, quantities: number[]) {
  const postJson = async (): Promise<unknown> => body;
  const lines = quantities.map((quantity, index) => ({ productId: `p-${index + 1}`, quantity }));
  const state = await loadCartViewState(new HttpCartRepository(postJson), lines);
  assert.equal(state.kind, "loaded");
  if (state.kind !== "loaded") throw new Error("not loaded");
  return state;
}

test("the item count label counts units and agrees in number", async () => {
  assert.equal((await load(quote, [2, 1])).itemCountLabel, "(3 items)");
  const single = { ...quote, lines: [{ ...quote.lines[1] }], subtotal: "15.00", total_discount: "0.00",
    shipping: "25.00", free_shipping_remaining: "45.00", estimated_total: "40.00" };
  assert.equal((await load(single, [1])).itemCountLabel, "(1 item)");
});

test("the total discount carries a leading minus and is absent when there is none", async () => {
  assert.equal((await load(quote, [2, 1])).discountLabel, "-US$13.34");
  const none = { ...quote, lines: [{ ...quote.lines[1] }], subtotal: "15.00", total_discount: "0.00",
    shipping: "25.00", free_shipping_remaining: "45.00", estimated_total: "40.00" };
  assert.equal((await load(none, [1])).discountLabel, null);
});

test("a line sold at list price shows no discount badge", async () => {
  const state = await load(quote, [2, 1]);
  assert.equal(state.lines[0].discountLabel, "18%");
  assert.equal(state.lines[1].discountLabel, null);
});
