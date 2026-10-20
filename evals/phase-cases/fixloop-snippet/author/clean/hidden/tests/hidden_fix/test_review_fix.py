import unittest

from shop.cart.http import build_cart_quote_handler

CATALOG = {"tee": {"price_cents": 1500}, "mug": {"price_cents": 1200}}


class ReviewFixTest(unittest.TestCase):
    """The reviewer's valid finding: the label counts units, not lines."""

    def test_label_counts_units_across_lines(self):
        _, body = build_cart_quote_handler(CATALOG)(
            {"lines": [{"product_id": "tee", "quantity": 2}, {"product_id": "mug", "quantity": 1}]})
        self.assertEqual(body["item_count_label"], "(3 items)")

    def test_label_counts_units_of_one_line(self):
        _, body = build_cart_quote_handler(CATALOG)({"lines": [{"product_id": "mug", "quantity": 3}]})
        self.assertEqual(body["item_count_label"], "(3 items)")


if __name__ == "__main__":
    unittest.main()
