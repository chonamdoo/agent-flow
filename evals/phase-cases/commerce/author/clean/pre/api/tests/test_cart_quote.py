import unittest

from shop.cart.http import build_cart_quote_handler

CATALOG = {
    "p-1": {"name": "Toner", "price_cents": 2840, "list_price_cents": 3550},
    "p-2": {"name": "Serum", "price_cents": 1500, "list_price_cents": 1500},
}


class CartQuoteTest(unittest.TestCase):
    def setUp(self):
        self.handler = build_cart_quote_handler(CATALOG)

    def test_quotes_lines_and_totals_as_two_decimal_strings(self):
        status, body = self.handler({"lines": [{"product_id": "p-1", "quantity": 1}]})
        self.assertEqual(status, 200)
        self.assertEqual(body["currency"], "USD")
        self.assertEqual(body["lines"], [{
            "product_id": "p-1", "name": "Toner", "quantity": 1, "unit_price": "28.40",
            "list_price": "35.50", "line_total": "28.40", "discount_rate": 20,
        }])
        self.assertEqual(body["subtotal"], "28.40")
        self.assertEqual(body["total_discount"], "7.10")
        self.assertEqual(body["shipping"], "25.00")
        self.assertEqual(body["free_shipping_threshold"], "60.00")
        self.assertEqual(body["free_shipping_remaining"], "31.60")
        self.assertEqual(body["estimated_total"], "53.40")

    def test_a_cart_above_the_threshold_ships_free(self):
        status, body = self.handler({"lines": [
            {"product_id": "p-1", "quantity": 2}, {"product_id": "p-2", "quantity": 1},
        ]})
        self.assertEqual(status, 200)
        self.assertEqual(body["subtotal"], "71.80")
        self.assertEqual(body["total_discount"], "14.20")
        self.assertEqual(body["shipping"], "0.00")
        self.assertEqual(body["free_shipping_remaining"], "0.00")
        self.assertEqual(body["estimated_total"], "71.80")
        self.assertEqual(body["lines"][1]["discount_rate"], 0)

    def test_quantities_outside_one_to_five_are_rejected(self):
        for quantity in (0, 6):
            status, body = self.handler({"lines": [{"product_id": "p-1", "quantity": quantity}]})
            self.assertEqual(status, 422)
            self.assertEqual(body, {"error": "quantity_out_of_range", "product_id": "p-1"})

    def test_an_unknown_product_is_not_found(self):
        status, body = self.handler({"lines": [{"product_id": "p-9", "quantity": 1}]})
        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "unknown_product", "product_id": "p-9"})


if __name__ == "__main__":
    unittest.main()
