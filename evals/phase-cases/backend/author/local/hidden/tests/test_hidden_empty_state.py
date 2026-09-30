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
]


class EmptyStateTest(unittest.TestCase):
    def test_empty_result_carries_the_planned_message(self):
        handler = build_list_orders_handler(ROWS)
        response = handler({"customer_id": "c-1", "status": "cancelled"})
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["body"]["orders"], [])
        self.assertEqual(response["body"].get("message"), "Nothing to show - no orders match these filters.")

    def test_customer_without_orders_gets_the_same_message(self):
        handler = build_list_orders_handler(ROWS)
        response = handler({"customer_id": "c-9"})
        self.assertEqual(response["status"], 200)
        self.assertEqual(response["body"]["orders"], [])
        self.assertEqual(response["body"].get("message"), "Nothing to show - no orders match these filters.")


if __name__ == "__main__":
    unittest.main()
