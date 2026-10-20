import unittest

from app import list_orders


class OrdersTest(unittest.TestCase):
    def test_unauthorized(self):
        self.assertEqual(list_orders(auth=False), (401, {"error": "unauthorized"}))

    def test_authorized(self):
        status, body = list_orders(auth=True)
        self.assertEqual(status, 200)
        self.assertEqual(body["orders"][0]["order_id"], "o-1")


if __name__ == "__main__":
    unittest.main()
