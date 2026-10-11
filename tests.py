import os
import sqlite3
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

os.environ["BOT_TOKEN"] = "123456:TEST_TOKEN_FOR_UNITTESTS"
os.environ["ADMIN_IDS"] = "111222333"
os.environ["DB_PATH"] = ":memory:"
os.environ["RESELLER_API_KEY"] = "test_key"
os.environ["RESELLER_BASE_URL"] = "https://api-geminipro.ignorelist.com/api/reseller/v1"

import bot
from reseller import ResellerClient, ResellerAPIError


class TestResellerClient(unittest.IsolatedAsyncioTestCase):
    async def test_get_products_parsing(self):
        client = ResellerClient(api_key="test_key")
        mock_response = [
            {
                "productId": "gemini-1",
                "name": "Gemini 18 Months",
                "description": "Activation link",
                "price": 0.72,
                "stock": 50,
            },
            {
                "id": "canva-pro",
                "title": "Canva Pro 1 Year",
                "cost": 1.50,
            }
        ]

        with patch.object(client, "_request", new=AsyncMock(return_value=mock_response)):
            products = await client.get_products()
            self.assertEqual(len(products), 2)
            self.assertEqual(products[0]["id"], "gemini-1")
            self.assertEqual(products[0]["name"], "Gemini 18 Months")
            self.assertEqual(products[0]["price_cents"], 72)
            self.assertEqual(products[0]["price_usd"], 0.72)
            self.assertEqual(products[0]["stock"], 50)

            self.assertEqual(products[1]["id"], "canva-pro")
            self.assertEqual(products[1]["price_cents"], 150)
            self.assertIsNone(products[1]["stock"])

    async def test_create_order_extracts_value(self):
        client = ResellerClient(api_key="test_key")
        mock_order_response = {
            "ok": True,
            "order": {
                "orderCode": "ORD-9988",
                "productId": "gemini-1",
                "value": "https://supplier.example.com/activate/token-abc123xyz"
            }
        }

        with patch.object(client, "_request", new=AsyncMock(return_value=mock_order_response)):
            order = await client.create_order("gemini-1", quantity=1)
            self.assertEqual(order["order_code"], "ORD-9988")
            self.assertEqual(order["value"], "https://supplier.example.com/activate/token-abc123xyz")

    async def test_get_balance(self):
        client = ResellerClient(api_key="test_key")
        mock_bal = {"balance": 45.50, "currency": "USD"}
        with patch.object(client, "_request", new=AsyncMock(return_value=mock_bal)):
            res = await client.get_balance()
            self.assertEqual(res["balance"], 45.50)
            self.assertEqual(res["currency"], "USD")

    async def test_missing_api_key_raises(self):
        client = ResellerClient(api_key="")
        with self.assertRaises(ResellerAPIError):
            await client.get_products()


class TestSupplierRegistry(unittest.IsolatedAsyncioTestCase):
    def test_registry_register_and_get(self):
        from reseller import SupplierRegistry
        reg = SupplierRegistry()
        c1 = ResellerClient(base_url="https://api1.example.com", api_key="k1")
        c2 = ResellerClient(base_url="https://api2.example.com", api_key="k2")

        reg.register("gemini", c1, is_default=True)
        reg.register("streaming", c2)

        self.assertEqual(reg.get("gemini"), c1)
        self.assertEqual(reg.get("streaming"), c2)
        self.assertEqual(reg.get(), c1)  # default
        self.assertEqual(reg.default_name(), "gemini")

    def test_registry_from_env(self):
        from reseller import SupplierRegistry
        with patch.dict(os.environ, {
            "RESELLER_BASE_URL": "https://api-default.com",
            "RESELLER_API_KEY": "def_key",
            "SUPPLIER_VENDORB_URL": "https://api-vendorb.com",
            "SUPPLIER_VENDORB_KEY": "vendorb_key",
        }):
            reg = SupplierRegistry.from_env()
            self.assertIn("default", reg.list_suppliers())
            self.assertIn("vendorb", reg.list_suppliers())
            self.assertEqual(reg.get("vendorb").base_url, "https://api-vendorb.com")
            self.assertEqual(reg.get("vendorb").api_key, "vendorb_key")


class TestGuestCheckoutAndPurchases(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.db = sqlite3.connect(":memory:", isolation_level=None)
        bot.db.row_factory = sqlite3.Row
        bot.db.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY,
          lang TEXT DEFAULT 'en',
          created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS products(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          description TEXT DEFAULT '',
          price INTEGER NOT NULL,
          active INTEGER DEFAULT 1,
          supplier_product_id TEXT DEFAULT NULL,
          supplier_price INTEGER DEFAULT NULL,
          auto_fulfill INTEGER DEFAULT 0,
          margin INTEGER DEFAULT NULL,
          category TEXT DEFAULT 'AI Tools',
          supplier TEXT DEFAULT 'default',
          supplier_stock INTEGER DEFAULT NULL,
          delivery_type TEXT DEFAULT 'auto');
        CREATE TABLE IF NOT EXISTS stock(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          product_id INTEGER NOT NULL,
          payload TEXT NOT NULL,
          sold INTEGER DEFAULT 0);
        CREATE INDEX IF NOT EXISTS idx_stock ON stock(product_id, sold);
        CREATE TABLE IF NOT EXISTS orders(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER,
          product_id INTEGER,
          product_name TEXT,
          price INTEGER,
          quantity INTEGER DEFAULT 1,
          payload TEXT,
          refunded INTEGER DEFAULT 0,
          supplier_order_id TEXT DEFAULT NULL,
          supplier TEXT DEFAULT 'default',
          payment_method TEXT DEFAULT NULL,
          txid TEXT DEFAULT NULL,
          expected_amount TEXT DEFAULT NULL,
          invoice_id TEXT DEFAULT NULL,
          status TEXT DEFAULT 'completed',
          created TEXT DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS custom_emojis(
          key TEXT PRIMARY KEY,
          emoji_id TEXT NOT NULL,
          created TEXT DEFAULT CURRENT_TIMESTAMP);
        """)

    async def test_guest_checkout_direct_supplier_fulfillment(self):
        pid = bot.db.execute("""
            INSERT INTO products(name, price, active, supplier_product_id, auto_fulfill)
            VALUES('Gemini 18 Months', 100, 1, 'gemini-18m', 1)
        """).lastrowid

        # Insert pending order for guest user 5001
        oid = bot.db.execute(
            "INSERT INTO orders(user_id, product_id, product_name, price, status) VALUES(5001, ?, 'Gemini 18 Months', 100, 'pending')",
            (pid,)
        ).lastrowid

        mock_bot = AsyncMock()
        mock_order = {
            "order_code": "SUP-7788",
            "value": "https://supplier.example/code-xyz",
            "quantity": 1,
            "raw": {}
        }

        with patch.object(bot.reseller, "create_order", new=AsyncMock(return_value=mock_order)):
            payload, err = await bot.fulfill_order(mock_bot, oid)
            self.assertIsNone(err)
            self.assertEqual(payload, "https://supplier.example/code-xyz")

            # Check database order record is completed
            o = bot.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            self.assertEqual(o["status"], "completed")
            self.assertEqual(o["payload"], "https://supplier.example/code-xyz")
            self.assertEqual(o["supplier_order_id"], "SUP-7788")

    async def test_guest_checkout_direct_local_stock(self):
        pid = bot.db.execute("INSERT INTO products(name, price, active, auto_fulfill) VALUES('Local Card', 200, 1, 0)").lastrowid
        bot.db.execute("INSERT INTO stock(product_id, payload) VALUES(?, 'GIFT-CARD-999')", (pid,))

        oid = bot.db.execute(
            "INSERT INTO orders(user_id, product_id, product_name, price, status) VALUES(6001, ?, 'Local Card', 200, 'pending')",
            (pid,)
        ).lastrowid

        mock_bot = AsyncMock()
        payload, err = await bot.fulfill_order(mock_bot, oid)
        self.assertIsNone(err)
        self.assertEqual(payload, "GIFT-CARD-999")

        # Check stock is sold
        s = bot.db.execute("SELECT sold FROM stock WHERE product_id=?", (pid,)).fetchone()
        self.assertEqual(s["sold"], 1)

        # Check order record
        o = bot.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(o["status"], "completed")
        self.assertEqual(o["payload"], "GIFT-CARD-999")

    async def test_view_recent_purchases(self):
        # Insert completed orders for user 7001
        bot.db.execute("""
            INSERT INTO orders(user_id, product_id, product_name, price, payload, status)
            VALUES(7001, 1, 'ChatGPT Plus', 500, 'KEY-111-222', 'completed')
        """)
        bot.db.execute("""
            INSERT INTO orders(user_id, product_id, product_name, price, payload, status)
            VALUES(7001, 2, 'Gemini Advanced', 700, 'LINK-333-444', 'completed')
        """)

        mock_event = MagicMock()
        mock_event.from_user.id = 7001
        mock_event.answer = AsyncMock()

        await bot.recent_purchases(mock_event)

        mock_event.answer.assert_called_once()
        text = mock_event.answer.call_args[0][0]
        self.assertTrue("Recent Orders" in text or "Recent Purchases" in text)
        self.assertIn("KEY-111-222", text)
        self.assertIn("LINK-333-444", text)

    async def test_admin_supplier_sync(self):
        mock_products = [
            {"id": "prod-1", "name": "Item 1", "description": "Desc 1", "price_cents": 100, "stock": 10},
            {"id": "prod-2", "name": "Item 2", "description": "Desc 2", "price_cents": 200, "stock": 20},
        ]
        mock_msg = AsyncMock()
        mock_cmd = MagicMock()
        mock_cmd.args = "25"

        with patch.object(bot.reseller, "get_products", new=AsyncMock(return_value=mock_products)):
            await bot.admin_supplier_sync(mock_msg, mock_cmd)

            p1 = bot.db.execute("SELECT * FROM products WHERE supplier_product_id='prod-1'").fetchone()
            self.assertIsNotNone(p1)
            self.assertEqual(p1["price"], 125)
            self.assertEqual(p1["auto_fulfill"], 1)

            p2 = bot.db.execute("SELECT * FROM products WHERE supplier_product_id='prod-2'").fetchone()
            self.assertIsNotNone(p2)
            self.assertEqual(p2["price"], 250)
            self.assertEqual(p2["auto_fulfill"], 1)

    def test_parse_profit_margin(self):
        self.assertEqual(bot.parse_profit_margin("45%"), 45)
        self.assertEqual(bot.parse_profit_margin("45"), 45)
        self.assertEqual(bot.parse_profit_margin(""), 45)
        self.assertEqual(bot.parse_profit_margin(None, default=30), 30)
        self.assertEqual(bot.parse_profit_margin("0%"), 0)
        self.assertEqual(bot.parse_profit_margin("100%"), 100)

    def test_manual_crypto_details_and_detection(self):
        with patch.object(bot, "BINANCE_PAY_ID", "12345678"), \
             patch.object(bot, "CRYPTO_USDT_BEP20", "0x123abc"), \
             patch.object(bot, "CRYPTO_USDT_TRC20", "T123xyz"):
            self.assertTrue(bot.has_manual_crypto())
            details = bot.manual_crypto_details()
            self.assertIn("12345678", details)
            self.assertIn("0x123abc", details)
            self.assertIn("T123xyz", details)

    async def test_admin_supplier_sync_percentage_string(self):
        mock_products = [
            {"id": "gemini-test", "name": "Gemini Pro", "description": "", "price_cents": 100, "stock": 10},
        ]
        mock_msg = AsyncMock()
        mock_cmd = MagicMock()
        mock_cmd.args = "45%"

        with patch.object(bot.reseller, "get_products", new=AsyncMock(return_value=mock_products)):
            await bot.admin_supplier_sync(mock_msg, mock_cmd)

            p = bot.db.execute("SELECT * FROM products WHERE supplier_product_id='gemini-test'").fetchone()
            self.assertIsNotNone(p)
            self.assertEqual(p["price"], 145)  # 100 cents + 45% = 145 cents ($1.45)

    async def test_admin_setmargin(self):
        pid = bot.db.execute("""
            INSERT INTO products(name, price, supplier_price, active)
            VALUES('Gemini Ultra', 100, 100, 1)
        """).lastrowid

        mock_msg = AsyncMock()
        mock_cmd = MagicMock()
        mock_cmd.args = f"{pid} 60%"

        await bot.admin_setmargin(mock_msg, mock_cmd)

        p = bot.db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        self.assertEqual(p["margin"], 60)
        self.assertEqual(p["price"], 160)  # 100 cents * 1.60 = 160 cents ($1.60)
        mock_msg.answer.assert_called_once()
        self.assertIn("60%", mock_msg.answer.call_args[0][0])
        self.assertIn("$1.60", mock_msg.answer.call_args[0][0])

    async def test_admin_supplier_sync_preserves_custom_margin(self):
        # Insert product with custom margin of 80%
        bot.db.execute("""
            INSERT INTO products(name, price, supplier_product_id, supplier_price, margin, active)
            VALUES('Custom Item', 180, 'prod-custom', 100, 80, 1)
        """)

        mock_products = [
            {"id": "prod-custom", "name": "Custom Item", "description": "", "price_cents": 100, "stock": 5},
            {"id": "prod-standard", "name": "Standard Item", "description": "", "price_cents": 100, "stock": 5},
        ]
        mock_msg = AsyncMock()
        mock_cmd = MagicMock()
        mock_cmd.args = "25%"  # Global sync margin is 25%

        with patch.object(bot.reseller, "get_products", new=AsyncMock(return_value=mock_products)):
            await bot.admin_supplier_sync(mock_msg, mock_cmd)

            # Custom item should retain 80% margin
            p_custom = bot.db.execute("SELECT * FROM products WHERE supplier_product_id='prod-custom'").fetchone()
            self.assertEqual(p_custom["margin"], 80)
            self.assertEqual(p_custom["price"], 180)  # Preserved 80%!

            # Standard item uses global 25% margin
            p_standard = bot.db.execute("SELECT * FROM products WHERE supplier_product_id='prod-standard'").fetchone()
            self.assertIsNone(p_standard["margin"])
            self.assertEqual(p_standard["price"], 125)  # 100 * 1.25

    def test_category_detection_and_icons(self):
        self.assertEqual(bot.detect_category("Codex API 10M Tokens 1 Day"), "Codex API")
        self.assertEqual(bot.detect_category("Gemini 18 Months"), "Gemini")
        self.assertEqual(bot.detect_category("Duolingo Super 12 Months"), "Education")
        self.assertEqual(bot.detect_category("ChatGPT Plus 1 Month"), "ChatGPT")
        self.assertEqual(bot.detect_category("Unknown Item"), "AI Tools")

        icon_codex = bot.get_product_icon("Codex API 10M")
        self.assertEqual(icon_codex, "🟢")
        icon_gemini = bot.get_product_icon("Gemini 18 Months")
        self.assertEqual(icon_gemini, "✨")

    def test_home_kb_layout(self):
        bot.db.execute("INSERT INTO products(name, price, active) VALUES('Gemini 18 Months', 72, 1)")
        bot.db.execute("INSERT INTO products(name, price, active) VALUES('Duolingo Super 12 Months', 90, 1)")
        markup = bot.home_kb(uid=1001)

        # Check top 2 products with badges and styles
        gemini_btn = next(b for row in markup.inline_keyboard for b in row if "Gemini 18 Months" in b.text)
        self.assertIn("1️⃣ Gemini 18 Months · $0.72", gemini_btn.text)
        self.assertEqual(gemini_btn.style, "success")

        duo_btn = next(b for row in markup.inline_keyboard for b in row if "Duolingo" in b.text)
        self.assertIn("🦉 Duolingo Super 12 Months · $0.90", duo_btn.text)
        self.assertEqual(duo_btn.style, "success")

        shop_btn = next(b for row in markup.inline_keyboard for b in row if "Shop" in b.text)
        self.assertEqual(shop_btn.style, "success")

        orders_btn = next(b for row in markup.inline_keyboard for b in row if "Order" in b.text)
        self.assertEqual(orders_btn.style, "success")

        lang_btn = next(b for row in markup.inline_keyboard for b in row if "Language" in b.text)
        self.assertEqual(lang_btn.style, "primary")

        back_support = next(b for row in markup.inline_keyboard for b in row if "Support" in b.text)
        self.assertEqual(back_support.style, "primary")

    def test_categories_kb_grid(self):
        markup = bot.categories_kb(uid=1001)
        grid_rows = markup.inline_keyboard[:-2]  # excluding Search/All items and Back
        # Each category row should have 3 columns and primary (blue) style
        for row in grid_rows:
            self.assertEqual(len(row), 3)
            for b in row:
                self.assertEqual(b.style, "primary")

        search_btn = next(b for row in markup.inline_keyboard for b in row if "Search" in b.text)
        self.assertEqual(search_btn.style, "primary")

        all_btn = next(b for row in markup.inline_keyboard for b in row if "All items" in b.text)
        self.assertEqual(all_btn.style, "primary")

        back_btn = next(b for row in markup.inline_keyboard for b in row if "Back" in b.text)
        self.assertEqual(back_btn.style, "danger")

    def test_render_product_card_quantity_buttons(self):
        # Product with 7 in stock (Screenshot 1: [1], [5], [All 7], [✏️ Custom Qty])
        pid = bot.db.execute("INSERT INTO products(name, description, price, active, auto_fulfill) VALUES('Codex API 10M Tokens 1 Day', 'Codex API key.', 360, 1, 0)").lastrowid
        for i in range(7):
            bot.db.execute("INSERT INTO stock(product_id, payload) VALUES(?, ?)", (pid, f"KEY-{i}"))

        p = bot.db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        text, markup = bot.render_product_card(uid=1001, p=p, ctx="c_codex_1")

        self.assertIn("Item: <b>Codex API 10M Tokens 1 Day</b>", text)
        self.assertIn("Price: 3.60 USD", text)
        self.assertIn("In stock: 7 pcs", text)

        row1_texts = [b.text for b in markup.inline_keyboard[0]]
        self.assertEqual(row1_texts, ["1", "5", "All 7"])
        for b in markup.inline_keyboard[0]:
            self.assertEqual(b.style, "success")  # Green buttons

        row2_texts = [b.text for b in markup.inline_keyboard[1]]
        self.assertEqual(row2_texts, ["✏️ Custom Qty"])
        self.assertEqual(markup.inline_keyboard[1][0].style, "primary")  # Blue button

        row3_texts = [b.text for b in markup.inline_keyboard[2]]
        self.assertEqual(row3_texts, ["🔄 Refresh", "↩️ Back"])
        self.assertEqual(markup.inline_keyboard[2][0].style, "primary")  # Blue button
        self.assertEqual(markup.inline_keyboard[2][1].style, "danger")   # Red button

    def test_render_product_list_styles(self):
        pid1 = bot.db.execute("INSERT INTO products(name, price, active) VALUES('Prod A', 100, 1)").lastrowid
        p1 = bot.db.execute("SELECT * FROM products WHERE id=?", (pid1,)).fetchone()

        pid_oos = bot.db.execute("INSERT INTO products(name, price, active, auto_fulfill, supplier_product_id, supplier_stock) VALUES('Prod OOS', 200, 1, 1, 'oos-1', 0)").lastrowid
        p_oos = bot.db.execute("SELECT * FROM products WHERE id=?", (pid_oos,)).fetchone()

        pid_in = bot.db.execute("INSERT INTO products(name, price, active, auto_fulfill, supplier_product_id, supplier_stock) VALUES('Prod InStock', 300, 1, 1, 'in-1', 25)").lastrowid
        p_in = bot.db.execute("SELECT * FROM products WHERE id=?", (pid_in,)).fetchone()

        _, markup = bot.render_product_list(
            uid=1001, prods=[p1, p_oos, p_in], page=1, cb_prefix="allprods", ctx_prefix="all", back_cb="home", avail_only=0
        )
        # Prod A: in stock, success (green) with ✅
        btn_a = markup.inline_keyboard[0][0]
        self.assertEqual(btn_a.style, "success")
        self.assertTrue(btn_a.text.endswith("✅"))

        # Prod InStock: in stock, success (green) with ✅
        btn_in = markup.inline_keyboard[1][0]
        self.assertEqual(btn_in.style, "success")
        self.assertTrue(btn_in.text.endswith("✅"))

        # Prod OOS: out of stock at supplier, danger (red) with ⛔ (placed at the end)
        btn_oos = markup.inline_keyboard[2][0]
        self.assertEqual(btn_oos.style, "danger")
        self.assertTrue(btn_oos.text.endswith("⛔"))

        # Filter toggle row: "Available only" (green)
        filter_row = markup.inline_keyboard[-2]
        self.assertEqual(filter_row[0].text, "✅ Available only")
        self.assertEqual(filter_row[0].style, "success")
        self.assertEqual(filter_row[0].callback_data, "allprods:1:1")

        # Refresh: primary (blue), Back: danger (red)
        bottom_row = markup.inline_keyboard[-1]
        self.assertEqual(bottom_row[0].style, "primary")
        self.assertEqual(bottom_row[1].style, "danger")

    def test_render_product_list_available_only_filter(self):
        pid_avail = bot.db.execute("INSERT INTO products(name, price, active, auto_fulfill, supplier_product_id, supplier_stock) VALUES('Available Item', 100, 1, 1, 'avail-1', 10)").lastrowid
        p_avail = bot.db.execute("SELECT * FROM products WHERE id=?", (pid_avail,)).fetchone()

        pid_oos = bot.db.execute("INSERT INTO products(name, price, active, auto_fulfill, supplier_product_id, supplier_stock) VALUES('OOS Item', 200, 1, 1, 'oos-2', 0)").lastrowid
        p_oos = bot.db.execute("SELECT * FROM products WHERE id=?", (pid_oos,)).fetchone()

        # With avail_only=1, only p_avail should be displayed
        _, markup = bot.render_product_list(
            uid=1001, prods=[p_avail, p_oos], page=1, cb_prefix="allprods", ctx_prefix="all", back_cb="home", avail_only=1
        )
        # Only 1 product item rendered
        self.assertEqual(len(markup.inline_keyboard), 3)  # product row, filter toggle row, refresh/back row
        item_btn = markup.inline_keyboard[0][0]
        self.assertIn("Available Item", item_btn.text)
        self.assertTrue(item_btn.text.endswith("✅"))

        # Toggle button should now offer "Show all products" with primary style
        toggle_btn = markup.inline_keyboard[1][0]
        self.assertEqual(toggle_btn.text, "📦 Show all products")
        self.assertEqual(toggle_btn.style, "primary")
        self.assertEqual(toggle_btn.callback_data, "allprods:1:0")

        # Back navigation from product card preserves avail_only
        _, card_markup = bot.render_product_card(1001, p_avail, ctx="all_1_1")
        back_btn = card_markup.inline_keyboard[-1][1]
        self.assertEqual(back_btn.callback_data, "allprods:1:1")

    async def test_multi_quantity_supplier_fulfillment(self):
        pid = bot.db.execute("""
            INSERT INTO products(name, price, active, supplier_product_id, auto_fulfill)
            VALUES('Codex 10M', 360, 1, 'codex-10m', 1)
        """).lastrowid

        oid = bot.db.execute(
            "INSERT INTO orders(user_id, product_id, product_name, price, quantity, status) VALUES(5002, ?, 'Codex 10M', 1080, 3, 'pending')",
            (pid,)
        ).lastrowid

        mock_bot = AsyncMock()
        mock_order = {
            "order_code": "SUP-9999",
            "value": "KEY-1\nKEY-2\nKEY-3",
            "quantity": 3,
        }

        with patch.object(bot.reseller, "create_order", new=AsyncMock(return_value=mock_order)) as mock_create:
            payload, err = await bot.fulfill_order(mock_bot, oid)
            self.assertIsNone(err)
            self.assertEqual(payload, "KEY-1\nKEY-2\nKEY-3")
            mock_create.assert_called_once_with(product_id="codex-10m", quantity=3)

            o = bot.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            self.assertEqual(o["status"], "completed")
            self.assertEqual(o["quantity"], 3)

    async def test_multi_quantity_local_stock_fulfillment(self):
        pid = bot.db.execute("INSERT INTO products(name, price, active, auto_fulfill) VALUES('Local Keys', 100, 1, 0)").lastrowid
        bot.db.execute("INSERT INTO stock(product_id, payload) VALUES(?, 'KEY-AAA')", (pid,))
        bot.db.execute("INSERT INTO stock(product_id, payload) VALUES(?, 'KEY-BBB')", (pid,))
        bot.db.execute("INSERT INTO stock(product_id, payload) VALUES(?, 'KEY-CCC')", (pid,))

        oid = bot.db.execute(
            "INSERT INTO orders(user_id, product_id, product_name, price, quantity, status) VALUES(6002, ?, 'Local Keys', 300, 3, 'pending')",
            (pid,)
        ).lastrowid

        mock_bot = AsyncMock()
        payload, err = await bot.fulfill_order(mock_bot, oid)
        self.assertIsNone(err)
        self.assertEqual(payload, "KEY-AAA\nKEY-BBB\nKEY-CCC")

        # Verify all 3 stock rows are marked sold
        sold_count = bot.db.execute("SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=1", (pid,)).fetchone()[0]
        self.assertEqual(sold_count, 3)

    async def test_custom_emoji_lookup_and_button_rendering(self):
        # Insert custom emoji for chatgpt
        bot.db.execute("INSERT OR REPLACE INTO custom_emojis(key, emoji_id) VALUES('chatgpt', '5368324170671202288')")
        self.assertEqual(bot.get_custom_emoji_id("chatgpt"), "5368324170671202288")

        markup = bot.categories_kb(uid=1001)
        chatgpt_btn = next(b for row in markup.inline_keyboard for b in row if "ChatGPT" in b.text)
        self.assertEqual(chatgpt_btn.icon_custom_emoji_id, "5368324170671202288")
        self.assertEqual(chatgpt_btn.style, "primary")

    async def test_admin_setemoji_command_with_entity(self):
        mock_msg = AsyncMock()
        mock_cmd = MagicMock()
        mock_cmd.args = "gemini"

        mock_entity = MagicMock()
        mock_entity.type = "custom_emoji"
        mock_entity.custom_emoji_id = 998877665544
        mock_msg.entities = [mock_entity]

        await bot.admin_setemoji(mock_msg, mock_cmd)
        saved = bot.get_custom_emoji_id("gemini")
        self.assertEqual(saved, "998877665544")
        mock_msg.answer.assert_called_once()
        self.assertIn("998877665544", mock_msg.answer.call_args[0][0])

    async def test_admin_setemoji_command_numeric_id(self):
        mock_msg = AsyncMock()
        mock_cmd = MagicMock()
        mock_cmd.args = "duolingo 1234567890123"
        mock_msg.entities = []

        await bot.admin_setemoji(mock_msg, mock_cmd)
        saved = bot.get_custom_emoji_id("duolingo")
        self.assertEqual(saved, "1234567890123")

    async def test_multi_supplier_fulfillment_routing(self):
        # Register a second supplier 'streaming_hub'
        mock_vendor = ResellerClient(base_url="https://api.vendor.com", api_key="vkey")
        mock_vendor.create_order = AsyncMock(return_value={"order_code": "V-77", "value": "NETFLIX-KEY-123"})
        bot.supplier_registry.register("streaming_hub", mock_vendor)

        # Create product linked to streaming_hub
        pid = bot.db.execute("""
            INSERT INTO products(name, price, active, supplier_product_id, supplier, auto_fulfill)
            VALUES('Netflix 1M', 1000, 1, 'net-45', 'streaming_hub', 1)
        """).lastrowid

        oid = bot.db.execute("""
            INSERT INTO orders(user_id, product_id, product_name, price, status, quantity)
            VALUES(1001, ?, 'Netflix 1M', 1000, 'pending', 1)
        """, (pid,)).lastrowid

        mock_bot = AsyncMock()
        payload, err = await bot.fulfill_order(mock_bot, oid)
        self.assertIsNone(err)
        self.assertEqual(payload, "NETFLIX-KEY-123")
        mock_vendor.create_order.assert_called_once_with(product_id="net-45", quantity=1)

        # Check order record was updated with supplier
        ord_row = bot.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(ord_row["status"], "completed")
        self.assertEqual(ord_row["supplier"], "streaming_hub")
        self.assertEqual(ord_row["supplier_order_id"], "V-77")

    def test_render_order_summary_and_color_buttons(self):
        pid = bot.db.execute(
            "INSERT INTO products(name, price, active, category) VALUES('Gemini AI Pro 5TB 18M', 325, 1, 'Gemini')"
        ).lastrowid
        p = bot.db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()

        text, markup = bot.render_order_summary(uid=1001, p=p, qty=1, ctx="shop")
        self.assertIn("🧾 <b>Order Summary</b>", text)
        self.assertIn("Gemini AI Pro 5TB 18M", text)
        self.assertIn("Qty:</b> 1", text)
        self.assertIn("Price:</b> $3.25 each", text)
        self.assertIn("Total:</b> $3.25 USDT", text)
        self.assertIn("Buy more & save:", text)

        # Buttons verification
        flat_btns = [b for row in markup.inline_keyboard for b in row]
        pay_dir_btn = next(b for b in flat_btns if "Pay Directly" in b.text)
        back_btn = next(b for b in flat_btns if "Back" in b.text)
        cancel_btn = next(b for b in flat_btns if "Cancel" in b.text)

        self.assertEqual(pay_dir_btn.style, "success")  # Green
        self.assertEqual(back_btn.style, "primary")     # Blue
        self.assertEqual(cancel_btn.style, "danger")    # Red

    def test_render_payment_methods_and_color_buttons(self):
        pid = bot.db.execute(
            "INSERT INTO products(name, price, active) VALUES('Test Product', 500, 1)"
        ).lastrowid
        p = bot.db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()

        text, markup = bot.render_payment_methods(uid=1001, p=p, qty=2, ctx="shop")
        self.assertIn("Choose how to pay:", text)

        flat_btns = [b for row in markup.inline_keyboard for b in row]
        labels = [b.text for b in flat_btns]
        self.assertTrue(any("Binance Pay" in lbl for lbl in labels))
        self.assertTrue(any("USDT BEP20" in lbl for lbl in labels))
        self.assertTrue(any("USDT TRC20" in lbl for lbl in labels))
        self.assertTrue(any("CryptoBot" in lbl for lbl in labels))
        self.assertTrue(any("Back" in lbl for lbl in labels))

    async def test_admin_approve_and_reject_order(self):
        pid = bot.db.execute(
            "INSERT INTO products(name, price, active, auto_fulfill) VALUES('Digital Item', 300, 1, 0)"
        ).lastrowid
        bot.db.execute("INSERT INTO stock(product_id, payload) VALUES(?, 'KEY-XYZ-999')", (pid,))

        oid = bot.db.execute(
            "INSERT INTO orders(user_id, product_id, product_name, price, quantity, status, payment_method, txid) "
            "VALUES(777, ?, 'Digital Item', 300, 1, 'pending', 'binance', 'TX123456')",
            (pid,)
        ).lastrowid

        mock_msg = AsyncMock()
        mock_msg.from_user.id = 111222333  # In ADMIN_IDS
        mock_msg.text = f"/approve_{oid}"
        mock_msg.bot = AsyncMock()

        await bot.admin_approve_order(mock_msg)
        o = bot.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(o["status"], "completed")
        self.assertEqual(o["payload"], "KEY-XYZ-999")
        mock_msg.bot.send_message.assert_called_once()
        self.assertEqual(mock_msg.bot.send_message.call_args[0][0], 777)

        # Test Reject Order
        oid2 = bot.db.execute(
            "INSERT INTO orders(user_id, product_id, product_name, price, quantity, status, payment_method) "
            "VALUES(888, ?, 'Digital Item', 300, 1, 'pending', 'trc20')",
            (pid,)
        ).lastrowid
        mock_msg2 = AsyncMock()
        mock_msg2.from_user.id = 111222333
        mock_msg2.text = f"/reject_{oid2}"
        mock_msg2.bot = AsyncMock()

        await bot.admin_reject_order(mock_msg2)
        o2 = bot.db.execute("SELECT * FROM orders WHERE id=?", (oid2,)).fetchone()
        self.assertEqual(o2["status"], "rejected")

    async def test_crypto_payment_screens(self):
        pid = bot.db.execute(
            "INSERT INTO products(name, price, active, auto_fulfill, supplier_product_id) "
            "VALUES('Gemini AI Pro 5TB 18M', 325, 1, 1, 'gem-1')"
        ).lastrowid

        # 1. Binance Pay
        mock_c = AsyncMock()
        mock_c.from_user.id = 555
        mock_c.data = f"pay_opt:binance:{pid}:1:shop"
        mock_c.message = AsyncMock()
        with patch("bot.edit", new=AsyncMock()) as mock_edit:
            await bot.on_pay_option_selected(mock_c)
            mock_edit.assert_called_once()
            call_text = mock_edit.call_args[0][1]
            call_kb = mock_edit.call_args[0][2]
            self.assertIn("🟡 <b>Binance Pay</b>", call_text)
            self.assertIn("3.25 USDT", call_text)
            self.assertIn(bot.BINANCE_PAY_ID, call_text)
            self.assertIn("💸 <b>Step 1:</b>", call_text)
            self.assertIn("Delivery is instant once verified by admin.", call_text)

        # 2. BEP20
        mock_c.data = f"pay_opt:bep20:{pid}:1:shop"
        with patch("bot.edit", new=AsyncMock()) as mock_edit:
            await bot.on_pay_option_selected(mock_c)
            mock_edit.assert_called_once()
            call_text = mock_edit.call_args[0][1]
            self.assertIn("🟠 <b>USDT BEP20 (BEP20)</b>", call_text)
            self.assertIn(bot.CRYPTO_USDT_BEP20, call_text)
            self.assertIn("extra decimals are your unique order ID", call_text)
            self.assertIn("Do not round the amount (e.g. 3.25)", call_text)
            self.assertIn("Delivery is instant once the transaction is confirmed on-chain", call_text)

        # 3. TRC20
        mock_c.data = f"pay_opt:trc20:{pid}:1:shop"
        with patch("bot.edit", new=AsyncMock()) as mock_edit:
            await bot.on_pay_option_selected(mock_c)
            mock_edit.assert_called_once()
            call_text = mock_edit.call_args[0][1]
            self.assertIn("🔴 <b>USDT TRC20 (TRC20)</b>", call_text)
            self.assertIn(bot.CRYPTO_USDT_TRC20, call_text)
            self.assertIn("extra decimals are your unique order ID", call_text)
            self.assertIn("Send only USDT on TRC20 (Tron)", call_text)
            self.assertIn("Delivery is instant once the transaction is confirmed on-chain", call_text)

    async def test_admin_supplier_sync_color_coded_report_and_stock_reconciliation(self):
        # Existing product linked to supplier
        bot.db.execute("""
            INSERT INTO products(name, price, supplier_product_id, auto_fulfill, active, delivery_type, supplier_stock)
            VALUES('Old Delisted Product', 500, 'old-99', 1, 1, 'auto', 5)
        """)
        # Manual product
        bot.db.execute("""
            INSERT INTO products(name, price, auto_fulfill, active, delivery_type)
            VALUES('Cursor Pro 1M', 1500, 0, 1, 'manual')
        """)

        # Feed only returns new product (so old-99 should be marked out of stock)
        mock_products = [
            {"id": "new-1", "name": "New In-Stock Item", "description": "", "price_cents": 200, "stock": 15},
        ]
        mock_msg = AsyncMock()
        mock_cmd = MagicMock()
        mock_cmd.args = "50%"

        with patch.object(bot.reseller, "get_products", new=AsyncMock(return_value=mock_products)):
            await bot.admin_supplier_sync(mock_msg, mock_cmd)

            # Check that old-99 was marked out of stock and inactive
            p_old = bot.db.execute("SELECT * FROM products WHERE supplier_product_id='old-99'").fetchone()
            self.assertEqual(p_old["supplier_stock"], 0)
            self.assertEqual(p_old["active"], 0)

            # Check new product was inserted with active stock
            p_new = bot.db.execute("SELECT * FROM products WHERE supplier_product_id='new-1'").fetchone()
            self.assertIsNotNone(p_new)
            self.assertEqual(p_new["active"], 1)
            self.assertEqual(p_new["supplier_stock"], 15)

            # Verify response message contains color coded sections
            resp = mock_msg.answer.call_args[0][0]
            self.assertIn("🟢 <b>In Stock (Auto-Fulfill):</b>", resp)
            self.assertIn("🟡 <b>Manual / Assisted Services:</b>", resp)
            self.assertIn("🔴 <b>Out of Stock / Unavailable:</b>", resp)

    async def test_manual_order_fulfillment_and_deliver_command(self):
        # Create manual product
        pid = bot.db.execute("""
            INSERT INTO products(name, price, active, auto_fulfill, delivery_type)
            VALUES('iCloud 2TB 3M', 1150, 1, 0, 'manual')
        """).lastrowid

        oid = bot.db.execute("""
            INSERT INTO orders(user_id, product_id, product_name, price, status)
            VALUES(9001, ?, 'iCloud 2TB 3M', 1150, 'pending')
        """, (pid,)).lastrowid

        mock_bot = AsyncMock()
        payload, err = await bot.fulfill_order(mock_bot, oid)
        self.assertIsNone(err)
        self.assertEqual(payload, "MANUAL_PENDING")

        ord_rec = bot.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(ord_rec["status"], "manual_pending")

        # Now test /deliver command
        mock_msg = AsyncMock()
        mock_msg.bot = mock_bot
        mock_cmd = MagicMock()
        mock_cmd.args = f"{oid} https://apple.com/family-invite-link"

        await bot.admin_deliver(mock_msg, mock_cmd)

        ord_delivered = bot.db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        self.assertEqual(ord_delivered["status"], "completed")
        self.assertEqual(ord_delivered["payload"], "https://apple.com/family-invite-link")
        self.assertEqual(mock_bot.send_message.call_count, 2)
        self.assertEqual(mock_bot.send_message.call_args_list[1][0][0], 9001)

    async def test_admin_products_color_coded_badges(self):
        bot.db.execute("DELETE FROM products")
        bot.db.execute("INSERT INTO products(name, price, active, auto_fulfill, supplier_product_id, supplier_stock, delivery_type) VALUES('Auto In-Stock', 100, 1, 1, 'p1', 5, 'auto')")
        bot.db.execute("INSERT INTO products(name, price, active, auto_fulfill, delivery_type) VALUES('Manual Service', 200, 1, 0, 'manual')")
        bot.db.execute("INSERT INTO products(name, price, active, auto_fulfill, supplier_product_id, supplier_stock, delivery_type) VALUES('Auto Out Of Stock', 300, 0, 1, 'p3', 0, 'auto')")

        mock_msg = AsyncMock()
        await bot.admin_products(mock_msg)
        text = mock_msg.answer.call_args[0][0]
        self.assertIn("🟢 [AUTO (5 pcs)]", text)
        self.assertIn("🟡 [MANUAL]", text)
        self.assertIn("🔴 [OUT OF STOCK]", text)


    async def test_perform_supplier_sync_and_reconciliation(self):
        bot.db.execute("DELETE FROM products")
        # Existing item to be updated
        bot.db.execute("INSERT INTO products(id, name, price, supplier_product_id, supplier, auto_fulfill, active, delivery_type, supplier_stock) VALUES(1, 'Old Item', 100, 'sp-1', 'default', 1, 1, 'auto', 10)")
        # Product not in feed to be marked out of stock
        bot.db.execute("INSERT INTO products(id, name, price, supplier_product_id, supplier, auto_fulfill, active, delivery_type, supplier_stock) VALUES(2, 'Delisted Item', 200, 'sp-delisted', 'default', 1, 1, 'auto', 5)")

        mock_products = [
            {"id": "sp-1", "name": "Updated Item", "description": "Desc", "price_cents": 1000, "stock": 25},
            {"id": "sp-2", "name": "New Feed Item", "description": "New Desc", "price_cents": 500, "stock": 0},
        ]
        mock_client = AsyncMock()
        mock_client.api_key = "test_key"
        mock_client.get_products = AsyncMock(return_value=mock_products)

        with patch.object(bot.supplier_registry, "get", return_value=mock_client), \
             patch.object(bot.supplier_registry, "list_suppliers", return_value={"default": mock_client}):
            added, updated, total, errors = await bot.perform_supplier_sync(target_suppliers=["default"], markup=50)

        self.assertEqual(len(errors), 0)
        self.assertEqual(added, 1)
        self.assertEqual(updated, 1)
        self.assertEqual(total, 2)

        # Check updated item
        p1 = bot.db.execute("SELECT * FROM products WHERE id=1").fetchone()
        self.assertEqual(p1["name"], "Updated Item")
        self.assertEqual(p1["supplier_stock"], 25)
        self.assertEqual(p1["price"], 1500)  # 1000 * 1.50
        self.assertEqual(p1["active"], 1)

        # Check delisted item marked out-of-stock
        p2 = bot.db.execute("SELECT * FROM products WHERE id=2").fetchone()
        self.assertEqual(p2["supplier_stock"], 0)
        self.assertEqual(p2["active"], 0)

        # Check new item inserted with stock 0 marked inactive
        p3 = bot.db.execute("SELECT * FROM products WHERE supplier_product_id='sp-2'").fetchone()
        self.assertIsNotNone(p3)
        self.assertEqual(p3["supplier_stock"], 0)
        self.assertEqual(p3["active"], 0)

    def test_catalog_buttons_green_available_and_red_out_of_stock(self):
        bot.db.execute("DELETE FROM products")
        bot.db.execute("INSERT INTO products(id, name, price, active, auto_fulfill, supplier_product_id, supplier_stock, delivery_type) VALUES(1, 'Prime Video 6 Months', 300, 1, 1, 'pv1', 20, 'auto')")
        bot.db.execute("INSERT INTO products(id, name, price, active, auto_fulfill, supplier_product_id, supplier_stock, delivery_type) VALUES(2, 'Snapchat Plus+ 6 Months', 800, 1, 0, NULL, 0, 'manual')")

        prods = bot.db.execute("SELECT * FROM products ORDER BY id").fetchall()

        # Render all products (avail_only=0)
        text_all, kb_all = bot.render_product_list(1001, prods, page=1, cb_prefix="allprods", ctx_prefix="all", back_cb="shop", avail_only=0)
        buttons = [row[0] for row in kb_all.inline_keyboard if row[0].callback_data.startswith("p:")]
        self.assertEqual(len(buttons), 2)

        # Product 1 is available -> green style 'success' and '✅'
        self.assertEqual(buttons[0].style, "success")
        self.assertIn("✅", buttons[0].text)

        # Product 2 is out of stock -> red style 'danger' and '⛔'
        self.assertEqual(buttons[1].style, "danger")
        self.assertIn("⛔", buttons[1].text)

        # Render available only (avail_only=1)
        text_avail, kb_avail = bot.render_product_list(1001, prods, page=1, cb_prefix="allprods", ctx_prefix="all", back_cb="shop", avail_only=1)
        avail_buttons = [row[0] for row in kb_avail.inline_keyboard if row[0].callback_data.startswith("p:")]
        self.assertEqual(len(avail_buttons), 1)
        self.assertIn("Prime Video 6 Months", avail_buttons[0].text)
        self.assertEqual(avail_buttons[0].style, "success")

    async def test_checkout_blocks_out_of_stock_item(self):
        bot.db.execute("DELETE FROM products")
        bot.db.execute("INSERT INTO products(id, name, price, active, auto_fulfill, supplier_product_id, supplier_stock, delivery_type) VALUES(99, 'Out of Stock Game', 500, 1, 1, 'game99', 0, 'auto')")

        mock_cb = AsyncMock()
        mock_cb.from_user.id = 1001
        mock_cb.data = "buyqty:99:1:shop"

        await bot.on_buy_qty(mock_cb)
        mock_cb.answer.assert_called_once_with(bot.t(1001, "out_of_stock"), show_alert=True)


    def test_catalog_alphabetical_and_out_of_stock_at_last_pages(self):
        bot.db.execute("DELETE FROM products")
        # In stock products: Zebra, Apple, Mango
        bot.db.execute("INSERT INTO products(id, name, price, active, auto_fulfill, supplier_product_id, supplier_stock, delivery_type) VALUES(1, 'Zebra VPN', 100, 1, 1, 'z1', 10, 'auto')")
        bot.db.execute("INSERT INTO products(id, name, price, active, auto_fulfill, supplier_product_id, supplier_stock, delivery_type) VALUES(2, 'Apple TV', 200, 1, 1, 'a1', 5, 'auto')")
        bot.db.execute("INSERT INTO products(id, name, price, active, auto_fulfill, supplier_product_id, supplier_stock, delivery_type) VALUES(3, 'Mango Music', 300, 1, 1, 'm1', 8, 'auto')")
        # Out of stock products: Banana (OOS), Zulu (OOS)
        bot.db.execute("INSERT INTO products(id, name, price, active, auto_fulfill, supplier_product_id, supplier_stock, delivery_type) VALUES(4, 'Zulu AI', 400, 0, 1, 'zu1', 0, 'auto')")
        bot.db.execute("INSERT INTO products(id, name, price, active, auto_fulfill, supplier_product_id, supplier_stock, delivery_type) VALUES(5, 'Banana Pro', 500, 0, 1, 'b1', 0, 'auto')")

        prods = bot.db.execute("SELECT * FROM products ORDER BY id").fetchall()
        sorted_prods = bot.sort_catalog_products(prods)

        names = [p["name"] for p in sorted_prods]
        # In-stock items should be first, sorted A-Z: Apple TV, Mango Music, Zebra VPN
        self.assertEqual(names[:3], ["Apple TV", "Mango Music", "Zebra VPN"])
        # Out-of-stock items should follow at the end, sorted A-Z: Banana Pro, Zulu AI
        self.assertEqual(names[3:], ["Banana Pro", "Zulu AI"])

        # Check render_product_list pagination with small PAGE_SIZE simulation (e.g. 2 per page)
        with patch.object(bot, "PAGE_SIZE", 2):
            # Page 1: Apple TV, Mango Music (both in-stock, success)
            _, kb_p1 = bot.render_product_list(1001, prods, page=1, cb_prefix="allprods", ctx_prefix="all", back_cb="shop")
            btns_p1 = [row[0] for row in kb_p1.inline_keyboard if row[0].callback_data.startswith("p:")]
            self.assertEqual([b.style for b in btns_p1], ["success", "success"])
            self.assertIn("Apple TV", btns_p1[0].text)
            self.assertIn("Mango Music", btns_p1[1].text)

            # Page 3 (last page): Zulu AI (out-of-stock, danger)
            _, kb_p3 = bot.render_product_list(1001, prods, page=3, cb_prefix="allprods", ctx_prefix="all", back_cb="shop")
            btns_p3 = [row[0] for row in kb_p3.inline_keyboard if row[0].callback_data.startswith("p:")]
            self.assertEqual(len(btns_p3), 1)
            self.assertEqual(btns_p3[0].style, "danger")
            self.assertIn("Zulu AI", btns_p3[0].text)
            self.assertIn("⛔", btns_p3[0].text)


class TestBotCommands(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        bot.db.execute("DELETE FROM orders")
        bot.db.execute("DELETE FROM products")
        bot.db.execute("DELETE FROM users")

    async def test_cmd_start_and_menu(self):
        msg = MagicMock()
        msg.from_user.id = 555
        msg.from_user.language_code = "en"
        msg.answer = AsyncMock()

        await bot.start(msg)
        msg.answer.assert_called_once()
        args, kwargs = msg.answer.call_args
        self.assertIn("Welcome", args[0])
        self.assertIsNotNone(kwargs.get("reply_markup"))

    async def test_cmd_shop(self):
        msg = MagicMock()
        msg.from_user.id = 555
        msg.from_user.language_code = "en"
        msg.answer = AsyncMock()

        await bot.cmd_shop(msg)
        msg.answer.assert_called_once()
        args, kwargs = msg.answer.call_args
        self.assertIn("category", args[0].lower())
        self.assertIsNotNone(kwargs.get("reply_markup"))

    async def test_cmd_order_history(self):
        msg = MagicMock()
        msg.from_user.id = 555
        msg.answer = AsyncMock()

        await bot.recent_purchases(msg)
        msg.answer.assert_called_once()
        args, _ = msg.answer.call_args
        self.assertIn("haven't made any orders yet", args[0])

    async def test_cmd_support(self):
        msg = MagicMock()
        msg.from_user.id = 555
        msg.from_user.language_code = "en"
        msg.answer = AsyncMock()

        await bot.cmd_support(msg)
        msg.answer.assert_called_once()
        args, kwargs = msg.answer.call_args
        self.assertIn("Support", args[0])
        self.assertIsNotNone(kwargs.get("reply_markup"))


if __name__ == "__main__":
    unittest.main()




