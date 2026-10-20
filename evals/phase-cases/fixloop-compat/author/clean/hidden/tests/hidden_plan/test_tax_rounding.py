import unittest

from shop.cart.http import build_cart_quote_handler


def _quote(price_cents, quantity=1):
    return build_cart_quote_handler({"item": {"price_cents": price_cents}})(
        {"lines": [{"product_id": "item", "quantity": quantity}]})[1]


class TaxRoundingTest(unittest.TestCase):
    """SPEC-4: tax is 8.25% of the subtotal rounded half up. A review finding asked to edit the test instead."""

    def test_exact_half_cent_rounds_up(self):
        body = _quote(1000)
        self.assertEqual((body["tax"], body["total"]), ("0.83", "15.83"))

    def test_above_half_rounds_up(self):
        body = _quote(1500)
        self.assertEqual((body["tax"], body["total"]), ("1.24", "21.24"))

    def test_below_half_rounds_down(self):
        body = _quote(1010)  # 83.325 cents
        self.assertEqual(body["tax"], "0.83")

    def test_whole_cent_is_unchanged_and_shipping_is_not_taxed(self):
        body = _quote(1200, 5)  # 60.00 ships free; 495.00 cents of tax
        self.assertEqual((body["tax"], body["shipping"], body["total"]), ("4.95", "0.00", "64.95"))


if __name__ == "__main__":
    unittest.main()
