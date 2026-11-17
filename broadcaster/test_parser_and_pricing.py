import unittest
from pricing import PricingEngine
from parser import MessageParser

class TestParserAndPricing(unittest.TestCase):

    def setUp(self):
        # Initialise with standard rules
        self.pricing_engine = PricingEngine()
        self.parser = MessageParser(self.pricing_engine)

    def test_single_item_fixed_rule(self):
        # ChatGPT Plus has custom fixed markup of +2.50 in pricing_rules.json
        raw_msg = (
            "🔥 Restocked!\n\n"
            "ChatGPT Plus 1M Apple Pay\n\n"
            "💵 Price: 4.99 USD\n\n"
            "⚡ Instant delivery after payment\n"
            "⌛ Limited stock available"
        )
        result = self.parser.process_message(raw_msg)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "single")
        # 4.99 + 2.50 = 7.49
        self.assertEqual(result["items"][0]["new_price"], 7.49)
        self.assertIn("💵 Price: 7.49 USD", result["transformed_text"])
        self.assertIn("ChatGPT Plus 1M Apple Pay", result["transformed_text"])

    def test_single_item_percentage_rule(self):
        # Muse AI has custom 50% markup: 0.89 * 1.5 = 1.335 -> round(1.34)
        raw_msg = (
            "🔥 Restocked!\n\n"
            "Muse AI Account 31B Tokens\n\n"
            "💵 Price: 0.89 USD\n\n"
            "⚡ Instant delivery after payment\n"
            "⌛ Limited stock available"
        )
        result = self.parser.process_message(raw_msg)
        self.assertIsNotNone(result)
        self.assertEqual(result["items"][0]["new_price"], 1.34)
        self.assertIn("💵 Price: 1.34 USD", result["transformed_text"])

    def test_single_item_fixed_capcut(self):
        # CapCut Pro has fixed +1.10 markup: 0.90 + 1.10 = 2.00
        raw_msg = (
            "🔥 Restocked!\n\n"
            "CapCut Pro 7D 1200 Credits\n\n"
            "💵 Price: 0.90 USD\n\n"
            "⚡ Instant delivery after payment\n"
            "⌛ Limited stock available"
        )
        result = self.parser.process_message(raw_msg)
        self.assertIsNotNone(result)
        self.assertEqual(result["items"][0]["new_price"], 2.00)
        self.assertIn("💵 Price: 2.00 USD", result["transformed_text"])

    def test_list_format_bullet(self):
        raw_msg = (
            "🔥 Restocked\n\n"
            "These products are available again:\n\n"
            "• Muse AI Account 31B Tokens — 0.89 USD"
        )
        result = self.parser.process_message(raw_msg)
        self.assertIsNotNone(result)
        self.assertEqual(result["type"], "list")
        self.assertEqual(result["items"][0]["new_price"], 1.34)
        self.assertIn("• Muse AI Account 31B Tokens — 1.34 USD", result["transformed_text"])

    def test_unrelated_message_ignored(self):
        raw_msg = "Hey guys, our support is currently offline for maintenance."
        result = self.parser.process_message(raw_msg)
        self.assertIsNone(result)

if __name__ == "__main__":
    unittest.main()
