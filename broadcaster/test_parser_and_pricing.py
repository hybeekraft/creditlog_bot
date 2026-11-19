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
        # When supplier includes bulk pricing tiers, keep the bulk section and recalculate tiers
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
        # Bulk price section and bullet lines should be preserved with recalculated prices
        self.assertIn("📦 Bulk prices:", res["transformed_text"])
        self.assertIn("• 5+ pcs — 0.38 USD each", res["transformed_text"])
        self.assertIn("• 10+ pcs — 0.33 USD each", res["transformed_text"])
        self.assertIn("• 25+ pcs — 0.30 USD each", res["transformed_text"])
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

    def test_broadcaster_respects_store_db_pricing(self):
        import sqlite3, tempfile
        from pathlib import Path

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp:
            tmp_path = Path(tmp.name)

        try:
            conn = sqlite3.connect(tmp_path)
            c = conn.cursor()
            c.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
            c.execute("INSERT INTO settings (key, value) VALUES ('profit_margin', '50')")
            c.execute("""
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    active INTEGER,
                    price INTEGER,
                    fixed_price INTEGER,
                    margin INTEGER,
                    supplier_price INTEGER
                )
            """)
            # SKU 1: Locked fixed price at $15.00 via /setprice
            c.execute("INSERT INTO products VALUES (1, 'ChatGPT Plus 1M Apple Pay', 1, 1500, 1, 173, 550)")
            # SKU 2: Custom margin at 60% via /setmargin
            c.execute("INSERT INTO products VALUES (2, 'Custom Margin SKU', 1, 800, 0, 60, 500)")
            # SKU 3: Standard SKU without overrides (uses store margin 50%)
            c.execute("INSERT INTO products VALUES (3, 'Standard Margin SKU', 1, 750, 0, NULL, 500)")
            conn.commit()
            conn.close()

            engine = PricingEngine(db_path=tmp_path)
            parser = MessageParser(engine)

            # 1. Fixed price SKU: supplier announces $5.50, broadcast should announce locked $15.00
            msg_1 = "🔥 Restocked!\n\nChatGPT Plus 1M Apple Pay\n\n💵 Price: 5.50 USD"
            res_1 = parser.process_message(msg_1)
            self.assertEqual(res_1["items"][0]["new_price"], 15.00)
            self.assertIn("💵 Price: 15.00 USD", res_1["transformed_text"])

            # 2. Custom margin SKU: supplier announces $10.00, broadcast applies 60% -> $16.00
            msg_2 = "🔥 Restocked!\n\nCustom Margin SKU\n\n💵 Price: 10.00 USD"
            res_2 = parser.process_message(msg_2)
            self.assertEqual(res_2["items"][0]["new_price"], 16.00)
            self.assertIn("💵 Price: 16.00 USD", res_2["transformed_text"])

            # 3. Standard SKU: supplier announces $10.00, broadcast applies store sync margin 50% -> $15.00
            msg_3 = "🔥 Restocked!\n\nStandard Margin SKU\n\n💵 Price: 10.00 USD"
            res_3 = parser.process_message(msg_3)
            self.assertEqual(res_3["items"][0]["new_price"], 15.00)
            self.assertIn("💵 Price: 15.00 USD", res_3["transformed_text"])

            # 4. Brand new SKU not in DB: applies store sync margin 50% (20.00 * 1.50 = 30.00)
            msg_4 = "🔥 Restocked!\n\nUnlisted Future SKU\n\n💵 Price: 20.00 USD"
            res_4 = parser.process_message(msg_4)
            self.assertEqual(res_4["items"][0]["new_price"], 30.00)
            self.assertIn("💵 Price: 30.00 USD", res_4["transformed_text"])

        finally:
            tmp_path.unlink(missing_ok=True)

    def test_exact_match_priority_over_substring(self):
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
            conn.execute("""
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    active INTEGER,
                    price INTEGER,
                    fixed_price INTEGER,
                    margin INTEGER,
                    supplier_price INTEGER,
                    supplier_stock INTEGER
                )
            """)
            # SKU 1 is longer / substring of SKU 2
            conn.execute("INSERT INTO products VALUES (1, 'ChatGPT Plus 1M Apple Pay', 1, 1500, 1, 100, 500, 10)")
            conn.execute("INSERT INTO products VALUES (2, 'ChatGPT Plus 1M', 1, 1200, 1, 100, 400, 10)")
            conn.commit()
            conn.close()

            orig_db = b.DB_PATH
            b.DB_PATH = tmp_path
            try:
                # Querying exact SKU 2 name must return 2, never shadowed by SKU 1
                matched_id = b.find_store_product_id("ChatGPT Plus 1M")
                self.assertEqual(matched_id, 2)

                engine = PricingEngine(db_path=tmp_path)
                p = engine.find_store_product("ChatGPT Plus 1M")
                self.assertIsNotNone(p)
                self.assertEqual(p["id"], 2)
            finally:
                b.DB_PATH = orig_db
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_list_formats_hyphens_and_asterisks(self):
        # Lists using hyphens or asterisks and currency signs
        msg = (
            "🔥 Restocked:\n"
            "- Product A - $5.00\n"
            "* Product B: 10.00 USD"
        )
        res = self.parser.process_message(msg)
        self.assertIsNotNone(res)
        self.assertEqual(res["type"], "list")
        self.assertEqual(len(res["items"]), 2)
        self.assertEqual(res["items"][0]["name"], "Product A")
        self.assertEqual(res["items"][1]["name"], "Product B")
        self.assertIn("- Product A - 6.50 USD", res["transformed_text"])
        self.assertIn("* Product B : 13.00 USD", res["transformed_text"])

    def test_single_item_dollar_price_format(self):
        msg = (
            "🔥 Restocked!\n\n"
            "ChatGPT Plus\n\n"
            "Price: $10.00\n\n"
            "⚡ Instant delivery"
        )
        res = self.parser.process_message(msg)
        self.assertIsNotNone(res)
        self.assertEqual(res["type"], "single")
        # 10.00 + 2.50 = 12.50 USD (ChatGPT Plus fixed 2.50 rule in pricing_rules.json)
        self.assertEqual(res["items"][0]["new_price"], 12.50)
        self.assertIn("Price: 12.50 USD", res["transformed_text"])

    def test_sync_product_from_broadcast_updates_dynamic_price_and_stock(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        try:
            from broadcaster import broadcaster as b
        except ImportError:
            import broadcaster as b

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)

        try:
            conn = sqlite3.connect(tmp_path)
            conn.execute("""
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    price INTEGER,
                    active INTEGER,
                    supplier_product_id TEXT,
                    supplier_price INTEGER,
                    auto_fulfill INTEGER,
                    margin INTEGER,
                    category TEXT,
                    supplier TEXT,
                    supplier_stock INTEGER,
                    delivery_type TEXT,
                    fixed_price INTEGER DEFAULT 0
                )
            """)
            conn.execute("""
                INSERT INTO products (id, name, price, active, supplier_price, margin, fixed_price)
                VALUES (1, 'Muse AI Account 1B Tokens', 100, 0, 30, 45, 0)
            """)
            conn.commit()
            conn.close()

            orig_db = b.DB_PATH
            b.DB_PATH = tmp_path
            try:
                pid = b.sync_product_from_broadcast("Muse AI Account 1B Tokens", cost_usd=0.30, new_price_usd=0.44)
                self.assertEqual(pid, 1)

                conn = sqlite3.connect(tmp_path)
                row = conn.execute("SELECT price, supplier_price, active, supplier_stock FROM products WHERE id=1").fetchone()
                conn.close()

                self.assertEqual(row[0], 44)  # price updated to 44c ($0.44)
                self.assertEqual(row[1], 30)  # supplier_price updated to 30c ($0.30)
                self.assertEqual(row[2], 1)   # active set to 1
                self.assertEqual(row[3], 10)  # stock set to 10
            finally:
                b.DB_PATH = orig_db
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_sync_product_from_broadcast_respects_locked_fixed_price(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        try:
            from broadcaster import broadcaster as b
        except ImportError:
            import broadcaster as b

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)

        try:
            conn = sqlite3.connect(tmp_path)
            conn.execute("""
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    price INTEGER,
                    active INTEGER,
                    supplier_product_id TEXT,
                    supplier_price INTEGER,
                    auto_fulfill INTEGER,
                    margin INTEGER,
                    category TEXT,
                    supplier TEXT,
                    supplier_stock INTEGER,
                    delivery_type TEXT,
                    fixed_price INTEGER DEFAULT 0
                )
            """)
            # Product has manually locked fixed_price = 1 at 300 cents ($3.00)
            conn.execute("""
                INSERT INTO products (id, name, price, active, supplier_price, margin, fixed_price)
                VALUES (1, 'Muse AI 31B Gmail Stable', 300, 0, 150, 100, 1)
            """)
            conn.commit()
            conn.close()

            orig_db = b.DB_PATH
            b.DB_PATH = tmp_path
            try:
                pid = b.sync_product_from_broadcast("Muse AI 31B Gmail Stable", cost_usd=1.70, new_price_usd=2.47)
                self.assertEqual(pid, 1)

                conn = sqlite3.connect(tmp_path)
                row = conn.execute("SELECT price, supplier_price, active, supplier_stock, margin FROM products WHERE id=1").fetchone()
                conn.close()

                # Locked price MUST remain 300c ($3.00)
                self.assertEqual(row[0], 300)
                self.assertEqual(row[1], 170)  # supplier_price updated to 170c
                self.assertEqual(row[2], 1)    # active set to 1
                self.assertEqual(row[3], 10)   # stock set to 10
                # Effective margin recalculated: (300 - 170) / 170 = 76%
                self.assertEqual(row[4], 76)
            finally:
                b.DB_PATH = orig_db
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_sync_product_from_broadcast_auto_creates_new_sku(self):
        import sqlite3
        import tempfile
        from pathlib import Path
        try:
            from broadcaster import broadcaster as b
        except ImportError:
            import broadcaster as b

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            tmp_path = Path(f.name)

        try:
            conn = sqlite3.connect(tmp_path)
            conn.execute("""
                CREATE TABLE products (
                    id INTEGER PRIMARY KEY,
                    name TEXT,
                    description TEXT,
                    price INTEGER,
                    active INTEGER,
                    supplier_product_id TEXT,
                    supplier_price INTEGER,
                    auto_fulfill INTEGER,
                    margin INTEGER,
                    category TEXT,
                    supplier TEXT,
                    supplier_stock INTEGER,
                    delivery_type TEXT,
                    fixed_price INTEGER DEFAULT 0
                )
            """)
            conn.commit()
            conn.close()

            orig_db = b.DB_PATH
            b.DB_PATH = tmp_path
            try:
                new_id = b.sync_product_from_broadcast("Brand New AI Pro 1M", cost_usd=2.00, new_price_usd=3.00)
                self.assertIsNotNone(new_id)

                conn = sqlite3.connect(tmp_path)
                row = conn.execute("SELECT name, price, supplier_price, active, category FROM products WHERE id=?", (new_id,)).fetchone()
                conn.close()

                self.assertEqual(row[0], "Brand New AI Pro 1M")
                self.assertEqual(row[1], 300)
                self.assertEqual(row[2], 200)
                self.assertEqual(row[3], 1)
            finally:
                b.DB_PATH = orig_db
        finally:
            tmp_path.unlink(missing_ok=True)

    def test_supplier_screenshots_exact_flow(self):
        # 1. Screenshot 1: New product alert
        img1_msg = (
            "🆕 New product\n\n"
            "🌊 Muse AI 31B Gmail Stable\n\n"
            "💵 Price: 1.70 USD\n"
            "📦 Available now"
        )
        res1 = self.parser.process_message(img1_msg)
        self.assertIsNotNone(res1)
        self.assertEqual(res1["type"], "single")
        self.assertIn("🆕 New product", res1["transformed_text"])
        self.assertIn("Muse AI 31B Gmail Stable", res1["transformed_text"])
        self.assertIn("💵 Price:", res1["transformed_text"])

        # 2. Screenshot 2: Restocked with bulk prices recalculated with profit margin
        img2_msg = (
            "🔥 Restocked!\n\n"
            "🌊 Muse AI Account 1B Tokens\n\n"
            "💵 Price: 0.30 USD\n\n"
            "📦 Bulk prices:\n"
            "• 5+ pcs — 0.25 USD each\n"
            "• 10+ pcs — 0.22 USD each\n"
            "• 25+ pcs — 0.20 USD each\n\n"
            "⚡ Instant delivery after payment\n"
            "⌛ Limited stock available"
        )
        res2 = self.parser.process_message(img2_msg)
        self.assertIsNotNone(res2)
        self.assertEqual(res2["type"], "single")
        # Bulk prices must be preserved and recalculated with profit margin (50% rule: 0.38, 0.33, 0.30)
        self.assertIn("📦 Bulk prices:", res2["transformed_text"])
        self.assertIn("• 5+ pcs — 0.38 USD each", res2["transformed_text"])
        self.assertIn("• 10+ pcs — 0.33 USD each", res2["transformed_text"])
        self.assertIn("• 25+ pcs — 0.30 USD each", res2["transformed_text"])
        self.assertIn("🔥 Restocked!", res2["transformed_text"])
        self.assertIn("⚡ Instant delivery after payment", res2["transformed_text"])
        self.assertIn("⌛ Limited stock available", res2["transformed_text"])

if __name__ == "__main__":
    unittest.main()
