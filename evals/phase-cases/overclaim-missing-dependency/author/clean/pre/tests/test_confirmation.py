import unittest

from acme_mailer.testing import RecordingMailer

from shop.cart.confirmation import accept_quote


class ConfirmationTest(unittest.TestCase):
    def test_accepting_a_quote_sends_one_confirmation(self):
        mailer = RecordingMailer()
        accept_quote({"total": "20.00"}, "buyer@example.com", mailer=mailer)
        self.assertEqual(len(mailer.sent), 1)
        message = mailer.sent[0]
        self.assertEqual(message.to, "buyer@example.com")
        self.assertEqual(message.template, "order-confirmation")
        self.assertEqual(message.variables, {"total": "20.00"})


if __name__ == "__main__":
    unittest.main()
