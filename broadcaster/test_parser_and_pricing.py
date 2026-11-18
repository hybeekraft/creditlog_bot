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

    def test_lowercase_and_alert_restock_variations(self):
        # Case 1: lowercase "restocked!"
        raw_lower = (
            "⚡ restocked!\n\n"
            "ChatGPT Plus 1M Apple Pay\n\n"
            "💵 Price: 4.99 USD\n\n"
            "⚡ Instant delivery after payment"
        )
        res_lower = self.parser.process_message(raw_lower)
        self.assertIsNotNone(res_lower, "Lowercase 'restocked!' should be recognized")

        # Case 2: "RESTOCK ALERT"
        raw_alert = (
            "🚨 RESTOCK ALERT:\n\n"
            "Airtable Team 12 Months\n\n"
            "💵 Price: 10.00 USD"
        )
        res_alert = self.parser.process_message(raw_alert)
        self.assertIsNotNone(res_alert, "Uppercase 'RESTOCK ALERT' should be recognized")

    def test_new_product_format(self):
        # Matches supplier's "New product" broadcast format
        raw_msg = (
            "🆕 New product\n\n"
            "Ⓜ️ Muse AI 31B Gmail Stable\n\n"
            "💵 Price: 1.70 USD\n\n"
            "📦 Available now"
        )
        res = self.parser.process_message(raw_msg)
        self.assertIsNotNone(res, "'New product' announcement should be recognized")
        self.assertEqual(res["type"], "single")
        self.assertEqual(res["items"][0]["name"], "Ⓜ️ Muse AI 31B Gmail Stable")
        # 1.70 * 1.50 = 2.55 USD (Muse AI 50% markup rule)
        self.assertEqual(res["items"][0]["new_price"], 2.55)
        self.assertIn("💵 Price: 2.55 USD", res["transformed_text"])
        self.assertIn("🆕 New product", res["transformed_text"])
        self.assertIn("📦 Available now", res["transformed_text"])

    def test_single_item_with_bulk_prices(self):
        # When supplier includes bulk pricing tiers, omit the bulk section so it broadcasts as single unit
        raw_msg = (
            "🔥 Restocked!\n\n"
            "Ⓜ️ Muse AI Account 1B Tokens\n\n"
            "💵 Price: 0.30 USD\n\n"
            "📦 Bulk prices:\n"
            "• 5+ pcs — 0.25 USD each\n"
            "• 10+ pcs — 0.22 USD each\n"
            "• 25+ pcs — 0.20 USD each\n\n"
            "⚡ Instant delivery after payment\n"
            "⌛ Limited stock available"
        )
        res = self.parser.process_message(raw_msg)
        self.assertIsNotNone(res)
        self.assertEqual(res["type"], "single")
        self.assertEqual(res["items"][0]["name"], "Ⓜ️ Muse AI Account 1B Tokens")
        # 0.30 * 1.50 = 0.45 USD (Muse AI 50% markup rule)
        self.assertEqual(res["items"][0]["new_price"], 0.45)
        self.assertIn("💵 Price: 0.45 USD", res["transformed_text"])
        # Bulk price section and bullet lines should be omitted
        self.assertNotIn("Bulk prices", res["transformed_text"])
        self.assertNotIn("5+ pcs", res["transformed_text"])
        self.assertNotIn("10+ pcs", res["transformed_text"])
        self.assertNotIn("25+ pcs", res["transformed_text"])
        # Footer lines should be preserved
        self.assertIn("⚡ Instant delivery after payment", res["transformed_text"])
        self.assertIn("⌛ Limited stock available", res["transformed_text"])

    def test_brand_isolation_product_matching(self):
        try:
            from broadcaster import broadcaster as b
        except ImportError:
            import broadcaster as b
        import sqlite3, tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            conn = sqlite3.connect(tmp_path)
            conn.execute("CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, active INTEGER, supplier_stock INTEGER)")
            conn.execute("INSERT INTO products (id, name, active, supplier_stock) VALUES (1, 'CapCut Pro 1M', 1, 5)")
            conn.commit()
            conn.close()

            orig_db_path = b.DB_PATH
            b.DB_PATH = tmp_path
            try:
                # Generic 'Pro 1M' without brand should NOT match CapCut Pro 1M
                self.assertIsNone(b.find_store_product_id("Pro 1M"))
                # With brand 'CapCut', it should match
                self.assertEqual(b.find_store_product_id("CapCut Pro 1M"), 1)
            finally:
                b.DB_PATH = orig_db_path
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_broadcaster_pause_and_play(self):
        try:
            from broadcaster import broadcaster as b
        except ImportError:
            import broadcaster as b
        import sqlite3, tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            orig_db_path = b.DB_PATH
            b.DB_PATH = tmp_path
            try:
                # 1. Defaults to enabled when settings table doesn't exist
                self.assertTrue(b.is_broadcaster_enabled())

                # 2. Pause broadcaster
                b.set_broadcaster_enabled(False)
                self.assertFalse(b.is_broadcaster_enabled())

                # 3. Resume (play) broadcaster
                b.set_broadcaster_enabled(True)
                self.assertTrue(b.is_broadcaster_enabled())
            finally:
                b.DB_PATH = orig_db_path
        finally:
            tmp_path.unlink(missing_ok=True)

if __name__ == "__main__":
    unittest.main()
