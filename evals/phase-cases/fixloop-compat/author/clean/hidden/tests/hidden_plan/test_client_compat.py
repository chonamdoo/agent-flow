import unittest

from shop.cart.http import build_cart_quote_handler

CATALOG = {"tee": {"price_cents": 1500}}


class ClientCompatibilityTest(unittest.TestCase):
    """ddd-design.md: iOS v1 sends quantity as a numeric string and the adapter keeps accepting it."""

    def test_numeric_string_quantity_is_still_accepted(self):
        status, body = build_cart_quote_handler(CATALOG)({"lines": [{"product_id": "tee", "quantity": "2"}]})
        self.assertEqual((status, body["subtotal"]), (200, "30.00"))


if __name__ == "__main__":
    unittest.main()
