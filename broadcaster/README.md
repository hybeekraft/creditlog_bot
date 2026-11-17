# Creditlog Bot - Stock Broadcaster & Price Transformer

Automated Telegram listener and price transformer companion for `creditlog_bot`. It monitors restock announcements from supplier channels or 1-on-1 supplier bots, extracts products and wholesale prices, applies custom price rules (fixed or percentage), matches items with your local [`store.db`](file:///C:/Users/DELL/creditlog_bot/store.db), and broadcasts them to your channel with an inline purchase button deep-linking directly to that product in `@creditlogbot`.

---

## 📁 Directory Structure

```
creditlog_bot/
├── broadcaster/
│   ├── pricing_rules.json        # Your custom markup rules (fixed or percentage)
│   ├── pricing.py                # High-precision Decimal pricing engine
│   ├── parser.py                 # Message parser & format transformer
│   ├── broadcaster.py            # Telethon MTProto listener + channel publisher
│   ├── simulate_supplier_post.py # Local test runner with store.db matching
│   ├── test_parser_and_pricing.py# Automated unit test suite
│   ├── requirements.txt          # Broadcaster dependencies (telethon, python-dotenv)
│   ├── .env.example              # Credentials template
│   └── README.md
├── bot.py                        # Main creditlog_bot store
├── store.db                      # SQLite catalog & orders database
└── .env                          # Root store configuration
```

---

## ⚡ Integration Features with `creditlog_bot`

1. **Automatic Product Deep-Linking:**
   * When a restock alert arrives, `broadcaster.py` searches your active catalog in [`store.db`](file:///C:/Users/DELL/creditlog_bot/store.db).
   * If a matching product is found (e.g. `ChatGPT Plus`, `Airtable Team`, `CapCut Pro`), the broadcast button automatically deep-links to:
     `https://t.me/creditlogbot?start=p_<product_id>`
   * When customers click the button, your bot instantly opens that exact product's purchase screen!
2. **Shared Configuration:**
   * Automatically inherits `CHANNEL_URL` (e.g. `@credit_log`) and bot settings from your root `.env` without duplicate configuration.

---

## 🚀 Setup & Execution

### 1. Install Dependencies
```bash
pip install telethon python-dotenv
```
*(Or inside your venv: `C:\Users\DELL\creditlog_bot\.venv\Scripts\pip.exe install telethon`)*

### 2. Configure Telegram API Credentials
1. Visit [my.telegram.org](https://my.telegram.org) and log in with your Telegram account.
2. Go to **API development tools** and create an app (Title: `StockBroadcaster`).
3. Copy your `api_id` and `api_hash`.
4. Copy [.env.example](file:///C:/Users/DELL/creditlog_bot/broadcaster/.env.example) to `.env` inside `creditlog_bot/broadcaster/`:
   ```bash
   cp broadcaster/.env.example broadcaster/.env
   ```
5. Enter your `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, and supplier username(s) in `SOURCE_CHATS`:
   ```env
   TELEGRAM_API_ID=12345678
   TELEGRAM_API_HASH=your_api_hash
   SOURCE_CHATS=@supplier_channel,@supplier_bot
   ```

### 3. Generate String Session (Recommended for Cloud / Docker / Fly.io)
For headless cloud environments where an interactive SMS code prompt cannot run in the container:
```bash
python broadcaster/login_session.py
```
Copy the printed session string into `TELEGRAM_STRING_SESSION` in your `.env` or set it in Fly.io secrets:
```bash
fly secrets set TELEGRAM_STRING_SESSION="your_string_session"
```

---

## 💰 Configuring Markup Rules

Edit [pricing_rules.json](file:///C:/Users/DELL/creditlog_bot/broadcaster/pricing_rules.json):

```json
{
  "default_rule": {
    "type": "percentage",
    "value": 30.0
  },
  "custom_rules": [
    {
      "keyword": "ChatGPT Plus",
      "type": "fixed",
      "value": 2.50
    },
    {
      "keyword": "Muse AI",
      "type": "percentage",
      "value": 50.0
    },
    {
      "keyword": "CapCut Pro",
      "type": "fixed",
      "value": 1.10
    }
  ]
}
```

---

## 🧪 Testing Locally

Run the simulation to verify markup and `store.db` matching:
```bash
python broadcaster/simulate_supplier_post.py
```

Run unit tests:
```bash
python -m unittest discover -s broadcaster -p "test_*.py"
```

---

## 🔴 Running Live

```bash
python broadcaster/broadcaster.py
```
*(On first launch, enter your phone number and Telegram verification code to authenticate the listener session).*
