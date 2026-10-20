import unittest

from shop.cart.http import build_cart_quote_handler


class CartDecisionsTest(unittest.TestCase):
    def test_the_discount_rate_is_floored_not_rounded(self):
        handler = build_cart_quote_handler({"p-1": {"name": "Cream", "price_cents": 5766, "list_price_cents": 7100}})
        status, body = handler({"lines": [{"product_id": "p-1", "quantity": 1}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["lines"][0]["discount_rate"], 18)

    def test_free_shipping_starts_at_the_threshold(self):
        handler = build_cart_quote_handler({"p-1": {"name": "Mask", "price_cents": 2000, "list_price_cents": 2000}})
        status, body = handler({"lines": [{"product_id": "p-1", "quantity": 3}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["subtotal"], "60.00")
        self.assertEqual(body["shipping"], "0.00")
        self.assertEqual(body["free_shipping_remaining"], "0.00")
        self.assertEqual(body["estimated_total"], "60.00")


if __name__ == "__main__":
    unittest.main()
