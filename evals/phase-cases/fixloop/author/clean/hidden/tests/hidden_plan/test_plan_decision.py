import unittest

from shop.cart.http import build_cart_quote_handler

CATALOG = {"tee": {"price_cents": 1500}, "mug": {"price_cents": 1200}}


class ThresholdDecisionTest(unittest.TestCase):
    """The slice plan's product decision: exactly 60.00 ships free. A review finding said otherwise."""

    def test_exactly_the_threshold_ships_free(self):
        _, body = build_cart_quote_handler(CATALOG)({"lines": [{"product_id": "tee", "quantity": 4}]})
        self.assertEqual((body["subtotal"], body["shipping"], body["free_shipping"]), ("60.00", "0.00", True))

    def test_one_cent_below_pays_shipping(self):
        _, body = build_cart_quote_handler({"odd": {"price_cents": 5999}})(
            {"lines": [{"product_id": "odd", "quantity": 1}]})
        self.assertEqual((body["shipping"], body["free_shipping"]), ("5.00", False))


if __name__ == "__main__":
    unittest.main()
