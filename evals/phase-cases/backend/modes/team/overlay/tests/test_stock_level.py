import unittest

from app.container import build_stock_level_handler

ROWS = [{"sku": "mug", "on_hand": 12}, {"sku": "cap", "on_hand": 2}]


class StockLevelHandlerTest(unittest.TestCase):
    def setUp(self):
        self.handler = build_stock_level_handler(ROWS)

    def test_reports_level_and_state(self):
        self.assertEqual(
            self.handler({"sku": "cap"}),
            {"status": 200, "body": {"sku": "cap", "on_hand": 2, "state": "low"}},
        )

    def test_unknown_sku_is_not_found(self):
        self.assertEqual(self.handler({"sku": "hat"}), {"status": 404, "body": {"error": "unknown_sku"}})

    def test_requires_sku(self):
        self.assertEqual(self.handler({}), {"status": 400, "body": {"error": "missing_sku"}})


if __name__ == "__main__":
    unittest.main()
