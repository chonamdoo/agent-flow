import unittest

from shop.cart.http import build_cart_quote_handler

CATALOG = {
    "tee": {"price_cents": 1500},
    "mug": {"price_cents": 1200},
    "jacket": {"price_cents": 7500},
}


class CartQuoteTest(unittest.TestCase):
    def setUp(self):
        self.handle = build_cart_quote_handler(CATALOG)

    def test_single_unit_pays_shipping_below_the_threshold(self):
        status, body = self.handle({"lines": [{"product_id": "tee", "quantity": 1}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["subtotal"], "15.00")
        self.assertEqual(body["shipping"], "5.00")
        self.assertEqual(body["tax"], "1.24")
        self.assertEqual(body["total"], "21.24")
        self.assertFalse(body["free_shipping"])
        self.assertEqual(body["item_count_label"], "(1 item)")

    def test_large_order_ships_free(self):
        status, body = self.handle({"lines": [{"product_id": "jacket", "quantity": 1}]})
        self.assertEqual((status, body["shipping"], body["free_shipping"]), (200, "0.00", True))

    def test_unknown_product_is_404(self):
        status, body = self.handle({"lines": [{"product_id": "hat", "quantity": 1}]})
        self.assertEqual((status, body["error"]), (404, "unknown_product"))

    def test_quantity_outside_one_to_five_is_422(self):
        status, _ = self.handle({"lines": [{"product_id": "tee", "quantity": 6}]})
        self.assertEqual(status, 422)


if __name__ == "__main__":
    unittest.main()
