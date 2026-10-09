# Telegram Store Bot

Digital goods reseller bot with direct guest checkout, automatic delivery from our catalog, instant purchase history, 4 languages, and admin tools.

**No user accounts or deposit wallets required:** Customers browse products as guests, checkout directly, and can view their recent purchases anytime.

## 1. Create the bot
1. Open @BotFather → `/newbot` → copy the token.
2. Get your numeric Telegram ID from @userinfobot.

## 2. Configure
```bash
cp .env.example .env
nano .env          # fill BOT_TOKEN, ADMIN_IDS, RESELLER_API_KEY, NOWPayments keys, domain
```

## 3. Direct Checkout (NOWPayments Crypto or Direct Mode)
1. For automated crypto checkout: create an account on [nowpayments.io](https://nowpayments.io), add payout wallet, and get API key + IPN secret.
2. When a buyer checks out, a direct invoice is created for that specific order. Once paid, the digital item is delivered automatically!
3. If NOWPayments keys are left blank, the bot runs in direct/testing mode (immediate free delivery).


## 4. Run locally (polling; top-ups need a public HTTPS URL, e.g. ngrok)
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

### Option A: Automated Supplier Catalog (Recommended)
Add your supplier API key to `.env`:
```bash
RESELLER_BASE_URL=https://api-geminipro.ignorelist.com/api/reseller/v1
RESELLER_API_KEY=YOUR_API_KEY
RESELLER_MARKUP_PERCENT=20   # 20% profit margin
```

From your Telegram admin account:
- `/supplier` — Check supplier balance and account status.
- `/s_products` — View products available in the supplier catalog and their wholesale prices.
- `/s_sync 25` — Automatically import/sync all products from the supplier with a 25% profit margin!
- `/s_link <local_id> <supplier_id>` — Link an existing product to auto-fulfill via supplier.
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
