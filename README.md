# Telegram Store Bot

Digital goods reseller bot with direct guest checkout, automatic delivery from our catalog, instant purchase history, 4 languages, and admin tools.

**No user accounts or deposit wallets required:** Customers browse products as guests, checkout directly, and can view their recent purchases anytime.

## 1. Create the bot
1. Open @BotFather → `/newbot` → copy the token.
2. Get your numeric Telegram ID from @userinfobot.

## 2. Configure
```bash
cp .env.example .env
nano .env          # fill BOT_TOKEN, ADMIN_IDS, RESELLER_API_KEY, CRYPTOBOT_TOKEN, etc.
```

## 3. Direct Checkout (Crypto Payments & Direct Mode)
The bot supports multiple payment options for seamless guest checkout:
1. **@CryptoBot (Automated in-Telegram checkout):**
   - Obtain an API token from [@CryptoBot](https://t.me/CryptoBot) (`/pay` → Create App).
   - Set `CRYPTOBOT_TOKEN=your_token` in `.env`.
   - Buyers can pay directly inside Telegram with USDT, TON, BTC, etc., and check payment status with one tap for instant item delivery!
2. **Direct USDT & Binance Pay Transfer:**
   - Configure `BINANCE_PAY_ID`, `CRYPTO_USDT_BEP20`, and/or `CRYPTO_USDT_TRC20`.
   - Buyers can see your payment details and submit their TXID/receipt to support.
3. **Direct / Free Mode:**
   - If payment gateways are left unconfigured, the bot delivers items immediately (ideal for testing or free distribution).

## 4. Run locally
```bash
pip install -r requirements.txt
python bot.py
```

## 5. Deploy on a VPS (recommended)
Point a DNS A record for your domain at the server, then:
```bash
docker compose up -d --build
docker compose logs -f bot
```
Caddy obtains the HTTPS certificate automatically. Data lives in `./data/store.db`.

## 6. Products: Manual Stock or Automated Supplier Catalog

You can sell products via **manual stock** OR **automatically connect to our supplier catalog**:

### Option A: Automated Supplier Catalog (Single or Multi-Supplier)
Add your primary supplier and optional additional suppliers to `.env`:
```bash
# Primary Supplier (default)
RESELLER_BASE_URL=https://api-geminipro.ignorelist.com/api/reseller/v1
RESELLER_API_KEY=YOUR_API_KEY
PROFIT_MARGIN=45%   # Default profit margin (supports "45%" or 45)

# Optional Additional Suppliers (Generic Multi-Supplier Registry)
# Any SUPPLIER_<NAME>_KEY will be automatically registered!
SUPPLIER_STREAMING_URL=https://api-streaming.com/api/v1
SUPPLIER_STREAMING_KEY=your_streaming_api_key

SUPPLIER_GAMES_URL=https://api-games.com/api/v1
SUPPLIER_GAMES_KEY=your_games_api_key
```

From your Telegram admin account:
- `/supplier` or `/suppliers` — View all configured suppliers, their balances, and active product counts. Or `/supplier <name>` for detailed info.
- `/s_products [supplier]` — View catalog products and wholesale costs from a specific supplier (defaults to primary).
- `/s_sync [supplier_or_all] [margin_%]` — Automatically import/sync products:
  - `/s_sync` — Sync primary supplier with default margin.
  - `/s_sync 50%` — Sync primary supplier with 50% margin.
  - `/s_sync streaming 40%` — Sync a specific supplier.
  - `/s_sync all 45%` — Sync all configured suppliers in one command!
- `/setmargin <id> <margin_%>` — Set a custom profit margin for a specific product (e.g. `/setmargin 1 60%`). Overrides the default margin and is preserved across `/s_sync`!
- `/setprice <id> <price_usd>` — Set a direct fixed retail price for a specific product (e.g. `/setprice 1 2.99`).
- `/s_link <local_id> <supplier_id> [supplier_name]` — Link an existing product to auto-fulfill via a specific supplier.
- `/s_unlink <local_id>` — Unlink and switch back to manual stock.
- `/s_orders` — View recent supplier orders.

**How automatic delivery works:**
1. User clicks **Buy now** on any supplier product (`⚡ Auto`).
2. Bot contacts `POST /orders` with the product ID and quantity.
3. Bot retrieves `order.value` and delivers it instantly to the buyer.
4. If supplier is out of stock or returns an error, the buyer's balance is automatically preserved/refunded and admins receive an alert.

### Option B: Manual Stock
Send to your bot from an admin account:
```
/addproduct 0.72 Gemini 18 Months | Activation link delivered instantly
/addstock 1 first-key-or-link
second-key-or-link
third-key-or-link
/products
/admin          <- full command list
```

## Backups
```bash
sqlite3 data/store.db ".backup 'backup-$(date +%F).db'"
```
Add that to cron daily and copy backups off the server.

## Before going live checklist
- [ ] Test a checkout end to end and confirm digital goods are delivered instantly.
- [ ] Test `/orders` to confirm buyers can view their recent purchases.
- [ ] Test a manual product or run `/s_sync` to import supplier catalog.
- [ ] Write a short terms / refund policy and pin it in your news channel.
- [ ] Never share `.env`; rotate the bot token if it leaks.

## Using a different payment gateway
Only two places change: `create_order_invoice()` (create the invoice for the order) and
`ipn_handler()` (verify the webhook signature, then call `fulfill_order()` to deliver).

# creditlog_bot
