# CreditLog Telegram Store Bot

Digital goods reseller and automated store bot with direct guest checkout, supplier catalog integration, multi-currency support (USD / NGN), automatic fulfillment, and admin command center.

**No user accounts or deposit wallets required:** Customers browse products as guests, checkout directly via cards, crypto, or bank transfer, and view their purchase history anytime.

---

## 1. Quick Start

### 1. Create Your Telegram Bot
1. Open [@BotFather](https://t.me/BotFather) on Telegram and run `/newbot`.
2. Copy your bot token.
3. Obtain your numeric Telegram ID from [@userinfobot](https://t.me/userinfobot).

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env with your credentials:
# BOT_TOKEN, ADMIN_IDS, RESELLER_API_KEY, etc.
```

### 3. Run Locally
```bash
pip install -r requirements.txt
python bot.py
```

---

## 2. Payment Gateways Supported

1. **Bachs Payment Gateway (Cards, Bank Transfer, Mobile Money, Crypto):**
   - Get API key from [app.bachs.io](https://app.bachs.io).
   - Set `BACHS_API_KEY=sk_live_...` and `BACHS_CURRENCY=USD` (or NGN) in `.env` (or configure live via `/setbachs`).
   - Automatically redirects buyers back into Telegram after checkout.

2. **Direct Naira Bank Transfer:**
   - Configure your bank details in `.env` (`BANK_NAME`, `BANK_ACCOUNT_NUMBER`, `BANK_ACCOUNT_NAME`).
   - Or manage accounts directly via `/setbank`, `/addbank`, and `/delbank`.
   - Supports live USD/NGN exchange rates via `/setrate`.

3. **@CryptoBot (Automated in-Telegram crypto checkout):**
   - Obtain token from [@CryptoBot](https://t.me/CryptoBot) (`/pay` → Create App).
   - Set `CRYPTOBOT_TOKEN=...` in `.env`.
   - Instant verification and automatic order delivery.

4. **Direct USDT (TRC20 / BEP20) & Binance Pay:**
   - Set `BINANCE_PAY_ID`, `CRYPTO_USDT_BEP20`, and `CRYPTO_USDT_TRC20`.
   - Direct wallet payment with TXID submission.

---

## 3. Deployment Options

### Option A: Fly.io (Recommended Cloud Deployment)
Fly.io provides 24/7 hosting with encrypted persistent storage volumes for SQLite.

1. **Install flyctl and authenticate:**
   ```powershell
   irm https://fly.io/install.ps1 | iex
   fly auth login
   ```
2. **Create app configuration & persistent volume:**
   ```powershell
   fly launch --no-deploy
   fly volumes create bot_data --region iad --size 1
   ```
3. **Import secrets safely:**
   ```powershell
   python -c "import subprocess, os; fly = os.path.expanduser('~/.fly/bin/flyctl.exe'); [subprocess.run([fly, 'secrets', 'set', line.strip()]) for line in open('.env', encoding='utf-8') if '=' in line and not line.strip().startswith('#')]"
   ```
4. **Deploy:**
   ```powershell
   fly deploy --depot=false
   ```

### Option B: Docker Compose / VPS
```bash
docker compose up -d --build
docker compose logs -f bot
```

---

## 4. Supplier Catalog & Pricing Controls

### Automated Supplier Integration
Set your supplier API URL and key in `.env`:
```env
RESELLER_BASE_URL=https://api-vendor.com/api/reseller/v1
RESELLER_API_KEY=your_key
PROFIT_MARGIN=45%
```

### Pricing & Catalog Commands:
* `/s_sync [margin_%]` — Sync supplier catalog and apply profit markup (e.g. `/s_sync 40%`).
* `/setprice <product_id> <price>` — Set a manual fixed price lock (e.g. `/setprice 1 12.50` or `15000 NGN`).
* `/clearprices [product_id]` — Clear manual price overrides in one click and reset products back to dynamic supplier markup.
* `/setmargin <product_id> <margin_%>` — Custom margin for a single product (e.g. `/setmargin 1 50%`).
* `/products` — List all products, prices, and profit margins.
* `/export` / `/excel` — Download full product catalog in formatted Excel spreadsheet.

---

## 5. Complete Admin Commands Reference

| Command | Description |
| :--- | :--- |
| `/admin` | Open admin command center |
| `/products` | List all products, prices, stock, and margins |
| `/addproduct <price> <name>` | Create a new manual stock product |
| `/setprice <id> <price>` | Lock a fixed custom retail price |
| `/clearprices [id]` | Clear manual price locks & revert to dynamic supplier markup |
| `/setmargin <id> <margin>` | Set custom profit margin for a specific product |
| `/toggle <id>` | Enable or disable a product |
| `/addstock <id> <items>` | Add digital delivery keys/accounts (one per line) |
| `/stats` | View sales stats and revenue |
| `/export` / `/excel` | Download catalog spreadsheet (`.xlsx`) |
| `/backup` | Download complete SQLite database file (`.db`) |
| `/broadcast <text>` | Send broadcast announcement to all store buyers |
| `/mode [auto\|manual]` | Switch between Model A (instant fulfillment) and Model B (manual approval) |
| `/approve <order_id>` | Approve Model B order and purchase from supplier |
| `/deliver <order_id> <data>`| Deliver order payload manually to buyer |
| `/refund <order_id>` | Refund an order |
| `/s_products` | Browse raw supplier product catalog and costs |
| `/s_sync [margin]` | Re-sync supplier products and update inventory |
| `/s_orders` | View recent supplier orders and fulfillment status |
| `/supplier` | View supplier account balance and API status |
| `/setrate [rate]` | View or update USD to NGN exchange rate |
| `/setbachs [key]` | Configure Bachs Payment Gateway API Key |
| `/bachs` | View Bachs configuration and status |
| `/bachscurrency [USD\|NGN]` | Set Bachs checkout currency |
| `/setbank [Bank \| Acc \| Name]`| Configure Direct Bank Transfer |
| `/addbank <Bank \| Acc \| Name>`| Add additional bank account (multi-bank support) |
| `/delbank [id\|all]` | Remove a bank account |
| `/bank` | View active Bank Transfer details |
| `/fsub` | Manage mandatory join channels (Force-Subscribe) |
| `/setemoji <brand> <id>` | Configure official brand custom emojis |
| `/emojis` | View configured custom emoji mappings |
| `/delemoji <brand>` | Remove brand custom emoji |

---

## 6. Testing

Run the automated test suite covering all checkout flows, supplier integrations, currency converters, and edge cases:

```bash
python -m unittest tests.py
```
*(All 161 tests passing).*
