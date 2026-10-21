import unittest

from shop.cart.http import build_cart_quote_handler

CATALOG = {"jacket": {"price_cents": 7500}, "tee": {"price_cents": 1500}}


class DiscountTest(unittest.TestCase):
    def test_large_cart_gets_ten_percent_off(self):
        _, body = build_cart_quote_handler(CATALOG)({"lines": [{"product_id": "jacket", "quantity": 2}]})
        self.assertEqual((body["subtotal"], body["discount"], body["shipping"], body["total"]),
                         ("150.00", "15.00", "0.00", "135.00"))

    def test_small_cart_has_no_discount(self):
        _, body = build_cart_quote_handler(CATALOG)({"lines": [{"product_id": "tee", "quantity": 1}]})
        self.assertEqual((body["discount"], body["total"]), ("0.00", "20.00"))


if __name__ == "__main__":
    unittest.main()
