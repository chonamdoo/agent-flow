import unittest

from orders import build_list_orders_handler

ROWS = [
    {
        "order_id": "o-1",
        "customer_id": "c-1",
        "status": "paid",
        "total_cents": 1250,
        "placed_at": "2026-03-01T09:00:00Z",
    },
    {
        "order_id": "o-2",
        "customer_id": "c-1",
        "status": "shipped",
        "total_cents": 399,
        "placed_at": "2026-03-03T09:00:00Z",
    },
    {
        "order_id": "o-3",
        "customer_id": "c-1",
        "status": "paid",
        "total_cents": 10000,
        "placed_at": "2026-03-02T09:00:00Z",
    },
    {
        "order_id": "o-4",
        "customer_id": "c-2",
        "status": "paid",
        "total_cents": 500,
        "placed_at": "2026-03-04T09:00:00Z",
    },
]


class ListOrdersHandlerTest(unittest.TestCase):
    def setUp(self):
        self.handler = build_list_orders_handler(ROWS)

    def test_lists_only_the_requested_customers_orders(self):
        response = self.handler({"customer_id": "c-1"})
        self.assertEqual(response["status"], 200)
        self.assertEqual(sorted(order["id"] for order in response["body"]["orders"]), ["o-1", "o-2", "o-3"])

    def test_orders_are_newest_first(self):
        response = self.handler({"customer_id": "c-1"})
        self.assertEqual([order["id"] for order in response["body"]["orders"]], ["o-2", "o-3", "o-1"])

    def test_order_payload_shape_and_money_format(self):
        response = self.handler({"customer_id": "c-1", "status": "shipped"})
        self.assertEqual(
            response["body"]["orders"],
            [{"id": "o-2", "status": "shipped", "total": "3.99", "placed_at": "2026-03-03T09:00:00Z"}],
        )

    def test_filters_by_status(self):
        response = self.handler({"customer_id": "c-1", "status": "paid"})
        self.assertEqual(response["status"], 200)
        self.assertEqual({order["status"] for order in response["body"]["orders"]}, {"paid"})
        self.assertEqual(len(response["body"]["orders"]), 2)

    def test_rejects_unknown_status(self):
        response = self.handler({"customer_id": "c-1", "status": "lost"})
        self.assertEqual(response, {"status": 400, "body": {"error": "invalid_status"}})

    def test_requires_customer_id(self):
        response = self.handler({})
        self.assertEqual(response, {"status": 400, "body": {"error": "missing_customer_id"}})

    def test_empty_filter_result_carries_the_planned_message(self):
        response = self.handler({"customer_id": "c-1", "status": "cancelled"})
        self.assertEqual(
            response,
            {
                "status": 200,
                "body": {"orders": [], "message": "Nothing to show - no orders match these filters."},
            },
        )

    def test_customer_without_orders_gets_the_same_message(self):
        response = self.handler({"customer_id": "c-9"})
        self.assertEqual(
            response,
            {
                "status": 200,
                "body": {"orders": [], "message": "Nothing to show - no orders match these filters."},
            },
        )


if __name__ == "__main__":
    unittest.main()
