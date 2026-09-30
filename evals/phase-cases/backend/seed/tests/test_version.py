import unittest

from version import version_payload


class VersionPayloadTest(unittest.TestCase):
    def test_reports_service_and_version(self):
        self.assertEqual(version_payload(), {"service": "orders-service", "version": "0.1.0"})


if __name__ == "__main__":
    unittest.main()
