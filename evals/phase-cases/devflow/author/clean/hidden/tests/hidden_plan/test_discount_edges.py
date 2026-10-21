import unittest

from shop.cart.http import build_cart_quote_handler


class DiscountEdgeTest(unittest.TestCase):
    """Edges stated in the task text: 100.00 qualifies, the discount rounds down, shipping uses the subtotal."""

    def test_exactly_one_hundred_qualifies(self):
        _, body = build_cart_quote_handler({"p": {"price_cents": 5000}})({"lines": [{"product_id": "p", "quantity": 2}]})
        self.assertEqual((body["discount"], body["total"]), ("10.00", "90.00"))

    def test_discount_rounds_down_to_the_cent(self):
        _, body = build_cart_quote_handler({"p": {"price_cents": 10099}})({"lines": [{"product_id": "p", "quantity": 1}]})
        self.assertEqual((body["discount"], body["total"]), ("10.09", "90.90"))


if __name__ == "__main__":
    unittest.main()
