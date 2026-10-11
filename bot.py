"""
Telegram Reseller Store Bot.
Simplified guest checkout model:
  - No user accounts or wallet balance deposits.
  - Direct guest checkout for digital goods.
  - View recent purchases anytime.
  - Automated fulfillment from supplier catalog or local stock.
  - 4 languages: English, Español, Русский, العربية.
"""
import asyncio
import html
import json
import logging
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from dotenv import load_dotenv

from reseller import ResellerClient, ResellerAPIError, SupplierRegistry
import onchain

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("store")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}

def is_admin(m: Message) -> bool:
    return bool(m.from_user and m.from_user.id in ADMIN_IDS)
SUPPORT_URL = os.environ.get("SUPPORT_URL", "https://t.me/your_support")
CHANNEL_URL = os.environ.get("CHANNEL_URL", "https://t.me/your_channel")
LOW_STOCK = int(os.environ.get("LOW_STOCK", "2"))
DB_PATH = os.environ.get("DB_PATH", "store.db")
PORT = int(os.environ.get("PORT", "8080"))

def parse_profit_margin(val: Optional[str], default: int = 45) -> int:
    if not val:
        return default
    cleaned = "".join(c for c in str(val) if c.isdigit())
    return int(cleaned) if cleaned else default

# Profit Margin (default 45%)
raw_margin = os.environ.get("PROFIT_MARGIN") or os.environ.get("RESELLER_MARKUP_PERCENT") or "45%"
PROFIT_MARGIN = parse_profit_margin(raw_margin, default=45)
RESELLER_MARKUP_PERCENT = PROFIT_MARGIN

# Reseller / Supplier Catalog API (Multi-Supplier Registry)
RESELLER_BASE_URL = (os.environ.get("RESELLER_BASE_URL") or "https://api-geminipro.ignorelist.com/api/reseller/v1").rstrip("/")
RESELLER_API_KEY = os.environ.get("RESELLER_API_KEY", "")
supplier_registry = SupplierRegistry.from_env()
reseller = supplier_registry.get("default") or ResellerClient(base_url=RESELLER_BASE_URL, api_key=RESELLER_API_KEY)
if "default" not in supplier_registry.list_suppliers():
    supplier_registry.register("default", reseller, is_default=True)

# Crypto Payments
CRYPTOBOT_TOKEN = os.environ.get("CRYPTOBOT_TOKEN", "")
BINANCE_PAY_ID = (os.environ.get("BINANCE_PAY_ID", "") or "").strip()
if not BINANCE_PAY_ID or BINANCE_PAY_ID.startswith("your_"):
    BINANCE_PAY_ID = "76177351"  # Default from store design

# USDT Addresses
CRYPTO_USDT_BEP20 = (os.environ.get("CRYPTO_USDT_BEP20", "") or "").strip()
if not CRYPTO_USDT_BEP20 or CRYPTO_USDT_BEP20.startswith("0xYour") or CRYPTO_USDT_BEP20.startswith("0x4B20"):
    CRYPTO_USDT_BEP20 = "0xe37301d1cb7E1d479a1F1A78d6A2EAed317412Db"

CRYPTO_USDT_TRC20 = (os.environ.get("CRYPTO_USDT_TRC20", "") or "").strip()
if not CRYPTO_USDT_TRC20 or CRYPTO_USDT_TRC20.startswith("TYour") or CRYPTO_USDT_TRC20.startswith("TR7NH"):
    CRYPTO_USDT_TRC20 = "TUaDYFz2fBRXSRDm9d6kN9j2J7bi7ZsBLW"

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
db = sqlite3.connect(DB_PATH, isolation_level=None)
db.row_factory = sqlite3.Row
db.executescript("""
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
""")

# --------------------------------------------------------------------------
# Categories & Icons Definition (24 Categories from Reference Design)
# --------------------------------------------------------------------------
CATEGORIES = [
    ("chatgpt", "ChatGPT", "✳️"),
    ("gemini", "Gemini", "✨"),
    ("grok", "Grok", "𝕏"),
    ("codex", "Codex API", "🟢"),
    ("claude", "Claude", "✴️"),
    ("kimi", "Kimi", "🌘"),
    ("video_ai", "Video AI", "▶️"),
    ("elevenlabs", "ElevenLabs", "⏸️"),
    ("ai_tools", "AI Tools", "⚡"),
    ("qwen", "Qwen API", "🟣"),
    ("deepseek", "DeepSeek AI", "🐳"),
    ("cursor", "Cursor API", "⬛"),
    ("adobe", "Adobe", "🅰️"),
    ("autodesk", "Autodesk", "📐"),
    ("design", "Design", "✂️"),
    ("office", "Office", "🪟"),
    ("education", "Education", "🦉"),
    ("streaming", "Streaming", "🎬"),
    ("music", "Music", "🎧"),
    ("vpn", "VPN", "🛡️"),
    ("security", "Security", "🔒"),
    ("games", "Games", "🎮"),
    ("social", "Social", "💼"),
    ("discord", "Discord", "👾"),
]
CAT_BY_KEY = {k: (name, icon) for k, name, icon in CATEGORIES}
CAT_KEY_BY_NAME = {name.lower(): k for k, name, icon in CATEGORIES}


def detect_category(name: str) -> str:
    n = name.lower()
    if "codex" in n or "astra" in n:
        return "Codex API"
    if "chatgpt" in n or "gpt" in n or "openai" in n:
        return "ChatGPT"
    if "gemini" in n:
        return "Gemini"
    if "grok" in n:
        return "Grok"
    if "claude" in n or "anthropic" in n:
        return "Claude"
    if "kimi" in n or "moonshot" in n:
        return "Kimi"
    if any(k in n for k in ["sora", "runway", "pika", "kling", "luma", "video ai", "capcut"]):
        return "Video AI"
    if "elevenlabs" in n or "eleven" in n:
        return "ElevenLabs"
    if "qwen" in n or "tongyi" in n:
        return "Qwen API"
    if "deepseek" in n:
        return "DeepSeek AI"
    if "cursor" in n:
        return "Cursor API"
    if "adobe" in n or "photoshop" in n or "illustrator" in n:
        return "Adobe"
    if "autodesk" in n or "autocad" in n or "revit" in n or "3ds max" in n or "maya" in n:
        return "Autodesk"
    if any(k in n for k in ["canva", "figma", "freepik", "envato", "design"]):
        return "Design"
    if any(k in n for k in ["office", "windows", "microsoft", "o365", "excel", "word", "zoom", "notion"]):
        return "Office"
    if any(k in n for k in ["duolingo", "coursera", "udemy", "education", "chegg", "quizlet", "cambly"]):
        return "Education"
    if any(k in n for k in ["netflix", "disney", "hbo", "prime video", "streaming", "hulu", "crunchyroll", "youtube"]):
        return "Streaming"
    if any(k in n for k in ["spotify", "apple music", "tidal", "deezer", "music"]):
        return "Music"
    if any(k in n for k in ["vpn", "nordvpn", "expressvpn", "surfshark", "mullvad", "hma", "proton", "windscribe"]):
        return "VPN"
    if any(k in n for k in ["security", "antivirus", "malwarebytes", "kaspersky", "eset"]):
        return "Security"
    if any(k in n for k in ["steam", "playstation", "xbox", "nintendo", "game", "roblox"]):
        return "Games"
    if any(k in n for k in ["twitter", "x.com", "instagram", "tiktok", "facebook", "linkedin", "social"]):
        return "Social"
    if any(k in n for k in ["discord", "nitro", "decor", "gifting"]):
        return "Discord"
    return "AI Tools"


def get_product_icon(name: str, cat_name: Optional[str] = None) -> str:
    n = name.lower()
    if "snapchat" in n:
        return "👻"
    if "cursor" in n:
        return "🔲"
    if "elevenlabs" in n or "eleven" in n:
        return "⏸️"
    if "kimi" in n:
        return "🐇"
    if "capcut" in n:
        return "✂️"
    if "kling" in n:
        return "🎥"
    if "autodesk" in n or "autocad" in n:
        return "🅰️"
    if "prime video" in n or "prime" in n:
        return "📺"
    if "apple tv" in n:
        return "📺"
    if "fallout" in n or "game pass" in n or "xbox" in n:
        return "🎮"
    if "avast" in n or "mcafee" in n:
        return "🛡️"
    if "gamma" in n:
        return "✨"
    if "framer" in n or "magic patterns" in n or "mobbin" in n or "supercut" in n or "krea" in n or "leonardo" in n:
        return "🎨"
    if "n8n" in n:
        return "⚙️"
    if "brain.fm" in n:
        return "🎵"
    if "customer.io" in n:
        return "📬"
    if "fin ai" in n:
        return "🤖"
    if "warp" in n:
        return "💻"
    if "wink" in n:
        return "✨"
    if "readwise" in n:
        return "📖"
    if "veo" in n or "heygen" in n:
        return "🎥"
    if "icloud" in n:
        return "☁️"
    if "wordwall" in n or "quizlet" in n:
        return "📚"
    if "notion" in n:
        return "📝"
    if "gemini" in n:
        return "✨"
    if "qwen" in n:
        return "🔮"
    if "deepseek" in n:
        return "🐳"
    if "surfshark" in n:
        return "🦈"
    if "discord" in n or "nitro" in n:
        return "👾"
    if "gmail" in n:
        return "📧"
    if "figma" in n:
        return "🎨"
    if "scribd" in n or "ilovepdf" in n:
        return "📚"
    if "grammarly" in n or "quillbot" in n:
        return "✍️"
    if "pocket fm" in n:
        return "📻"
    if "flux" in n:
        return "⚡"
    if "hbo" in n:
        return "🎬"
    if "windows" in n:
        return "🪟"
    if "jetbrains" in n:
        return "💻"
    if "higgsfield" in n:
        return "🎥"
    if "openart" in n:
        return "🎨"
    if "coursera" in n or "kiro" in n:
        return "🎓"
    if "peacock" in n:
        return "🦚"
    if "manus" in n:
        return "🤖"
    category = cat_name or detect_category(name)
    key = CAT_KEY_BY_NAME.get(category.lower())
    if key and key in CAT_BY_KEY:
        return CAT_BY_KEY[key][1]
    for _, c_name, icon in CATEGORIES:
        if c_name.lower() == category.lower():
            return icon
    return "📦"


def get_products_for_category(key: str) -> list[sqlite3.Row]:
    cat_name, _ = CAT_BY_KEY.get(key, ("AI Tools", "⚡"))
    all_prods = db.execute("SELECT * FROM products ORDER BY id").fetchall()
    matched = []
    for p in all_prods:
        p_cat = (p["category"] or "").strip()
        if p_cat.lower() == cat_name.lower() or p_cat.lower() == key.lower():
            matched.append(p)
        elif not p_cat or p_cat == "AI Tools":
            if detect_category(p["name"]).lower() == cat_name.lower():
                matched.append(p)
    return matched


def migrate_db():
    cols_p = {row["name"] for row in db.execute("PRAGMA table_info(products)").fetchall()}
    if "supplier_product_id" not in cols_p:
        db.execute("ALTER TABLE products ADD COLUMN supplier_product_id TEXT DEFAULT NULL")
    if "supplier_price" not in cols_p:
        db.execute("ALTER TABLE products ADD COLUMN supplier_price INTEGER DEFAULT NULL")
    if "auto_fulfill" not in cols_p:
        db.execute("ALTER TABLE products ADD COLUMN auto_fulfill INTEGER DEFAULT 0")
    if "margin" not in cols_p:
        db.execute("ALTER TABLE products ADD COLUMN margin INTEGER DEFAULT NULL")
    if "category" not in cols_p:
        db.execute("ALTER TABLE products ADD COLUMN category TEXT DEFAULT 'AI Tools'")
    if "supplier" not in cols_p:
        db.execute("ALTER TABLE products ADD COLUMN supplier TEXT DEFAULT 'default'")
    if "supplier_stock" not in cols_p:
        db.execute("ALTER TABLE products ADD COLUMN supplier_stock INTEGER DEFAULT NULL")
    if "delivery_type" not in cols_p:
        db.execute("ALTER TABLE products ADD COLUMN delivery_type TEXT DEFAULT 'auto'")

    cols_o = {row["name"] for row in db.execute("PRAGMA table_info(orders)").fetchall()}
    if "supplier_order_id" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN supplier_order_id TEXT DEFAULT NULL")
    if "supplier" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN supplier TEXT DEFAULT 'default'")
    if "status" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'completed'")
    if "invoice_id" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN invoice_id TEXT DEFAULT NULL")
    if "quantity" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN quantity INTEGER DEFAULT 1")
    if "payment_method" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN payment_method TEXT DEFAULT NULL")
    if "txid" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN txid TEXT DEFAULT NULL")
    if "expected_amount" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN expected_amount TEXT DEFAULT NULL")

    # Update categories for existing products if not assigned
    for row in db.execute("SELECT id, name, category FROM products").fetchall():
        if not row["category"] or row["category"] == "AI Tools":
            detected = detect_category(row["name"])
            if detected != "AI Tools":
                db.execute("UPDATE products SET category=? WHERE id=?", (detected, row["id"]))

    # Ensure default showcase products (Gemini 18 Months and Duolingo Super 12 Months)
    g = db.execute("SELECT id FROM products WHERE name LIKE '%Gemini%'").fetchone()
    if not g:
        db.execute("""
            INSERT INTO products(name, description, price, category, auto_fulfill, active, delivery_type)
            VALUES('Gemini Pro 18 Months', 'Gemini Advanced & Pro 18 Months Subscription', 72, 'Gemini', 1, 1, 'auto')
        """)
    d = db.execute("SELECT id FROM products WHERE name LIKE '%Duolingo%'").fetchone()
    if not d:
        db.execute("""
            INSERT INTO products(name, description, price, category, auto_fulfill, active, delivery_type)
            VALUES('Duolingo Super 12M', 'Duolingo Super 12 Months Plan', 90, 'Education', 1, 1, 'auto')
        """)

    # Seed known manual / assisted services from reseller master catalog
    MANUAL_CATALOG_SEEDS = [
        # Screenshot Page 1
        ("ChatGPT Plus 1M Apple Pay", "Ready-made ChatGPT Plus account (Apple Pay). Format: Email | Password | 2FA.", 880, "ChatGPT"),
        ("ChatGPT K12 EDU 24M", "ChatGPT K12 EDU 24 Months student/educational account access.", 420, "ChatGPT"),
        ("ChatGPT Business Team 1M", "ChatGPT Team Workspace 1 Month access.", 1600, "ChatGPT"),
        ("ChatGPT Business Team 1M FW", "ChatGPT Team 1 Month Full Warranty access.", 1850, "ChatGPT"),
        ("ChatGPT Plus Your Mail 4M", "ChatGPT Plus 4 Months upgraded on your own personal email.", 4300, "ChatGPT"),
        ("ChatGPT Plus 4M", "ChatGPT Plus 4 Months subscription ready-made account.", 2449, "ChatGPT"),
        ("ChatGPT Go 1M", "ChatGPT Go 1 Month subscription access.", 385, "ChatGPT"),
        ("100$ API Claude 30D", "Claude API with $100 preloaded credits valid 30 days.", 250, "Claude"),
        ("500$ API Claude 30D", "Claude API with $500 preloaded credits valid 30 days.", 770, "Claude"),
        ("ElevenLabs Redeem 131K Credits", "ElevenLabs 131,000 character credits instant redeem code.", 300, "ElevenLabs"),
        ("ElevenReader Ultra 1Y Gift Link", "ElevenReader Ultra 1 Year official gift redeem link.", 550, "ElevenLabs"),
        ("Eleven Reader Ultra 12M", "ElevenReader Ultra 12 Months premium account access.", 250, "ElevenLabs"),
        ("Codex API 10M Tokens 1D", "Codex API 10M tokens 1-day high speed access.", 170, "Codex API"),
        ("Codex API 10M Tokens 1 Day", "Codex API 10M tokens 1 Day standard access.", 360, "Codex API"),
        ("Codex API 50M Tokens 2D", "Codex API 50M tokens 2-day access.", 400, "Codex API"),

        # Screenshot Page 2
        ("Grok API $10 1M", "Grok API $10 credit balance valid 1 month.", 250, "Grok"),
        ("Grok API $30 1M", "Grok API $30 credit balance valid 1 month.", 320, "Grok"),
        ("Grok API $50 1M", "Grok API $50 credit balance valid 1 month.", 500, "Grok"),
        ("Grok API $100 1M", "Grok API $100 credit balance valid 1 month.", 740, "Grok"),
        ("Claude API 10M Tokens 1 Day", "Claude API 10M tokens 1 Day access.", 200, "Claude"),
        ("Codex API 100M Tokens 3D", "Codex API 100M tokens 3-day access.", 600, "Codex API"),
        ("Gemini Pro 18 Months", "Google Gemini Pro & Advanced 18 Months access.", 65, "Gemini"),
        ("Grok API 4.5 HIGH/XHIGH 15M Tokens 1 Day", "Grok API 4.5 High/XHigh tier 15M tokens 1 day.", 250, "Grok"),
        ("Netflix 4K Private Slot 1M", "Netflix Ultra HD 4K private slot 1 month.", 450, "Streaming"),
        ("Zoom Pro 1M Ready-made Account", "Zoom Pro 1 Month ready-made account credentials.", 400, "Office"),
        ("Grok API 4.5 HIGH/XHIGH 100M Tokens 7 Days", "Grok API 4.5 High/XHigh tier 100M tokens 7 days.", 500, "Grok"),
        ("API Codex Astra $10 1M", "Codex Astra API $10 credits valid 1 month.", 300, "Codex API"),
        ("API Codex Astra $30 1M", "Codex Astra API $30 credits valid 1 month.", 450, "Codex API"),
        ("API Codex Astra $50 1M", "Codex Astra API $50 credits valid 1 month.", 550, "Codex API"),
        ("API Codex Astra $100 1M", "Codex Astra API $100 credits valid 1 month.", 900, "Codex API"),

        # Screenshot Page 3
        ("API Codex Astra $200 1M", "Codex Astra API $200 credits valid 1 month.", 1290, "Codex API"),
        ("API Codex Astra $300 1M", "Codex Astra API $300 credits valid 1 month.", 1800, "Codex API"),
        ("API Codex Astra $500 1M", "Codex Astra API $500 credits valid 1 month.", 3000, "Codex API"),
        ("API Codex Astra $1000 1M", "Codex Astra API $1000 credits valid 1 month.", 6000, "Codex API"),
        ("Super Grok 7 Days", "Super Grok premium subscription 7 days access.", 375, "Grok"),
        ("Zoom Pro 14D", "Zoom Pro 14 days subscription.", 200, "Office"),
        ("CDK Heavy Grok 1M", "Heavy Grok 1 Month CDK activation key.", 3499, "Grok"),
        ("Zoom Pro 28D", "Zoom Pro 28 days subscription.", 400, "Office"),
        ("CDK Grok Super 1M", "Grok Super 1 Month CDK activation key.", 2100, "Grok"),
        ("Claude API 50M Tokens 2 Days", "Claude API 50M tokens valid 2 days.", 400, "Claude"),
        ("Codex API 50M Tokens 1 Day", "Codex API 50M tokens valid 1 day.", 420, "Codex API"),
        ("Google AI Pro Gemini 3M", "Google AI Pro Gemini 3 Months subscription.", 1099, "Gemini"),
        ("Discord Nitro 1M Token", "Discord Nitro 1 Month direct token.", 400, "Discord"),
        ("Gemini Pro 3M", "Google Gemini Pro 3 Months subscription.", 100, "Gemini"),
        ("Discord Nitro 3M Token", "Discord Nitro 3 Months direct token.", 600, "Discord"),

        # Screenshot Page 4
        ("HMA VPN 20-30 Days", "HideMyAss VPN 20-30 days private account.", 250, "VPN"),
        ("O365 Ready Account Random 6-12M", "Office 365 ready-made account random 6-12 months.", 400, "Office"),
        ("Gemini API $10 1M", "Gemini API $10 credits valid 1 month.", 250, "Gemini"),
        ("Gemini API $30 1M", "Gemini API $30 credits valid 1 month.", 350, "Gemini"),
        ("Gemini API $50 1M", "Gemini API $50 credits valid 1 month.", 500, "Gemini"),
        ("Discord Nitro Trial 3M", "Discord Nitro 3 Months trial link.", 750, "Discord"),
        ("HMA Android/PC 20-30 Days", "HMA VPN Android & PC access 20-30 days.", 150, "VPN"),
        ("Microsoft 365 Family 5U 12M", "Microsoft 365 Family 5 Users 12 Months.", 1200, "Office"),
        ("Proton VPN Plus 1 Month", "Proton VPN Plus 1 Month subscription.", 325, "VPN"),
        ("Proton Unlimited 1M", "Proton Unlimited 1 Month premium account.", 550, "VPN"),
        ("Claude API 100M Tokens 3D", "Claude API 100M tokens valid 3 days.", 600, "Claude"),
        ("Codex API 100M Tokens 1 Day", "Codex API 100M tokens valid 1 day.", 500, "Codex API"),
        ("Discord Decor 66K Profile Effect", "Discord Profile Decor 66K points effect.", 600, "Discord"),
        ("Discord Decor 79K Profile Effect", "Discord Profile Decor 79K points effect.", 650, "Discord"),
        ("Discord Decor 92K Profile Effect", "Discord Profile Decor 92K points effect.", 750, "Discord"),

        # Screenshot Page 5
        ("Lovable Pro Lite 1 Year Link", "Lovable Pro Lite 1 Year activation invite link.", 800, "AI Tools"),
        ("Netflix Premium 4K Slot 1M", "Netflix Premium 4K UHD private profile 1 month.", 650, "Streaming"),
        ("CapCut Pro Team 1M", "CapCut Pro Team membership 1 month.", 270, "Video AI"),
        ("Duolingo Super 12M", "Duolingo Super 12 Months subscription.", 65, "Education"),
        ("Discord Gifting Badge x1", "Discord Gifting Badge x1 delivery.", 550, "Discord"),
        ("CapCut Pro Professional 28 Days", "CapCut Pro Professional 28 days activation.", 250, "Video AI"),
        ("Super Duolingo Your Mail 12 Months", "Duolingo Super 12 Months activated on your own email.", 900, "Education"),
        ("Claude API 500M Tokens 6D", "Claude API 500M tokens valid 6 days.", 2300, "Claude"),
        ("NordVPN Basic 3 Months Code", "NordVPN Basic 3 Months activation code.", 399, "VPN"),
        ("LinkedIn Career 3M", "LinkedIn Premium Career 3 Months subscription.", 220, "Social"),
        ("NordVPN 10 Devices 5M", "NordVPN 10 Devices support 5 Months.", 1500, "VPN"),
        ("Notion Business 3M", "Notion Business Workspace 3 Months.", 199, "Office"),
        ("NordVPN 10 Devices 3M", "NordVPN 10 Devices support 3 Months.", 1100, "VPN"),
        ("Notion Plus 12 Months", "Notion Plus 12 Months subscription.", 250, "Office"),
        ("Canva Pro Add to Team 12 Months", "Canva Pro Add to Team 12 Months membership.", 450, "Design"),

        # Screenshot Page 6
        ("Adobe Express 4M", "Adobe Express 4 Months subscription.", 881, "Design"),
        ("Scribd Premium 1M", "Scribd Premium 1 Month subscription.", 220, "Education"),
        ("Adobe Express 12M", "Adobe Express 12 Months workspace plan.", 350, "Design"),
        ("Adobe Express 1 Year", "Adobe Express 1 Year access.", 50, "Design"),
        ("Adobe Express 12M Ready-made Account", "Adobe Express 12 Months ready-made account.", 400, "Design"),
        ("Adobe All Apps 7D", "Adobe All Apps 7 Days activation access.", 367, "Adobe"),
        ("iLovePDF Premium 1 Year", "iLovePDF Premium 1 Year subscription.", 150, "Office"),
        ("ExpressVPN 1M", "ExpressVPN 1 Month high speed VPN.", 587, "VPN"),
        ("ExpressVPN 3 Days", "ExpressVPN 3 Days quick access.", 110, "VPN"),
        ("YouTube Premium 3M ROW", "YouTube Premium 3 Months Rest of World.", 574, "Streaming"),
        ("YouTube Premium 12M Provider Account", "YouTube Premium 12 Months provider account.", 850, "Streaming"),
        ("AutoCAD EDU 12M", "AutoCAD Educational 12 Months license.", 980, "Autodesk"),
        ("YouTube Premium 3M Vietnam", "YouTube Premium 3 Months Vietnam region.", 1000, "Streaming"),
        ("YouTube Premium 3M Abroad", "YouTube Premium 3 Months Abroad region.", 1250, "Streaming"),
        ("YouTube Premium 3 Months", "YouTube Premium 3 Months individual plan.", 350, "Streaming"),

        # Screenshot Page 7
        ("Codex API 500M Tokens 6 Days", "Codex API 500M tokens valid 6 days.", 2980, "Codex API"),
        ("Wordwall Pro 1 Month", "Wordwall Pro 1 Month educational license.", 380, "Education"),
        ("GitHub Student Developer Pack", "GitHub Student Developer Pack activation.", 1500, "Office"),
        ("Grammarly AI Pro 1M", "Grammarly AI Pro 1 Month premium writing.", 550, "Office"),
        ("Krea Basic 5100 Credits 1M", "Krea Basic 5,100 Credits 1 Month video & image.", 550, "AI Tools"),
        ("Figma Pro EDU 2 Years", "Figma Pro Educational 2 Years membership.", 600, "Design"),
        ("Pocket FM Premium 3M", "Pocket FM Premium 3 Months audiobook plan.", 60, "Music"),
        ("Figma EDU 12M", "Figma Educational 12 Months membership.", 1250, "Design"),
        ("Leonardo AI Essential 8500 Credits 1M", "Leonardo AI Essential 8,500 Credits 1 Month.", 300, "Video AI"),
        ("Apple Music 6M", "Apple Music individual 6 Months subscription.", 200, "Music"),
        ("ElevenLabs Creator 3 Months", "ElevenLabs Creator 3 Months full access.", 1749, "ElevenLabs"),
        ("Claude API 10M Tokens 1D", "Claude API 10M tokens valid 1 day.", 300, "Claude"),
        ("Claude API 50M Tokens 1 Day", "Claude API 50M tokens valid 1 day.", 500, "Claude"),
        ("Claude API 100M Tokens 1 Day", "Claude API 100M tokens valid 1 day.", 600, "Claude"),
        ("Claude API 100M Tokens 3 Days", "Claude API 100M tokens valid 3 days.", 1000, "Claude"),

        # Screenshot Page 8
        ("Prime Video 6 Months", "Amazon Prime Video 6 Months private subscription.", 300, "Streaming"),
        ("Apple TV+ 1M Canada", "Apple TV+ 1 Month Canada region access.", 50, "Streaming"),
        ("Claude API 500M Tokens 1 Day", "Claude API 500M tokens 1 Day high volume access.", 3300, "Claude"),
        ("Microsoft Office 365 Plus 1 Year", "Microsoft Office 365 Plus 1 Year account license.", 160, "Office"),
        ("Apple TV+ 12 Months Activation Link", "Apple TV+ 12 Months official activation link.", 290, "Streaming"),
        ("CapCut Pro 1 Month", "CapCut Pro 1 Month membership access.", 250, "Design"),
        ("CapCut Pro 1M 1600 AI Credits", "CapCut Pro 1 Month with 1600 AI credits.", 250, "Design"),
        ("Fallout 76 MS Store", "Fallout 76 Microsoft Store digital edition key.", 259, "Games"),
        ("PC Game Pass 1M", "Xbox PC Game Pass 1 Month subscription.", 816, "Games"),
        ("Avast Premium Security 1Y", "Avast Premium Security 1 Year activation license.", 539, "Security"),
        ("McAfee Total Protection 1Y", "McAfee Total Protection 1 Year security subscription.", 625, "Security"),
        ("McAfee AntiVirus 1 Device 1Y", "McAfee AntiVirus 1 Device 1 Year license.", 656, "Security"),
        ("CapCut Pro 6M Personal", "CapCut Pro 6 Months personal account subscription.", 1450, "Design"),
        ("Gamma Pro 1M", "Gamma Pro AI presentation maker 1 Month subscription.", 1400, "AI Tools"),
        ("Gamma Pro 12 Months", "Gamma Pro AI presentation maker 12 Months subscription.", 2700, "AI Tools"),

        # Screenshot Page 9
        ("Gamma Pro 12M Coupon Code", "Gamma Pro 12 Months coupon redeem code.", 4600, "AI Tools"),
        ("Framer Pro 12 Months", "Framer Pro 12 Months workspace plan.", 1700, "Design"),
        ("Prime Video Ultra 6M 6 Profiles", "Prime Video Ultra 6 Months 6 Profiles private slot.", 260, "Streaming"),
        ("ElevenLabs Free 10k Credits", "ElevenLabs 10,000 character credits access.", 150, "ElevenLabs"),
        ("Gamma Plus 1M", "Gamma Plus 1 Month subscription access.", 800, "AI Tools"),
        ("Gamma AI Plus 1 Month", "Gamma AI Plus 1 Month subscription access.", 1100, "AI Tools"),
        ("ElevenLabs Creator 12 Months", "ElevenLabs Creator 12 Months full plan.", 4298, "ElevenLabs"),
        ("Magic Patterns Starter 12M", "Magic Patterns Starter 12 Months subscription.", 350, "Design"),
        ("Mobbin 10x Seat 12M", "Mobbin Design Library 10x Seat 12 Months.", 800, "Design"),
        ("N8N Starter 12 Months", "n8n Cloud Starter 12 Months subscription.", 1500, "AI Tools"),
        ("N8N Starter 1 Year", "n8n Cloud Starter 1 Year subscription.", 1500, "AI Tools"),
        ("Brain.fm 1 Year", "Brain.fm Focus Music 1 Year subscription.", 670, "Music"),
        ("Kling Pro 3K3 Credits 1M", "Kling Pro 3,300 Credits 1 Month video plan.", 2600, "Video AI"),
        ("Customer.io Essentials 1Y", "Customer.io Essentials 1 Year account access.", 350, "Office"),
        ("Fin AI + Advanced", "Fin AI Copilot + Advanced 1 Month access.", 350, "AI Tools"),

        # Screenshot Page 10
        ("Warp Build 1 Year", "Warp Terminal Build 1 Year membership.", 1100, "Office"),
        ("Wink Smile+ 7 Days", "Wink Smile+ Video AI 7 Days access.", 230, "Design"),
        ("Readwise 1 Year", "Readwise Reader 1 Year subscription.", 550, "Office"),
        ("Supercut Pro 10 Seats 12M", "Supercut Pro 10 Seats 12 Months team license.", 550, "Design"),
        ("Autodesk Admin Access", "Autodesk Admin Console Access license.", 1400, "Autodesk"),
        ("Veo Ultra X20 25K Credits Share 1M", "Veo Ultra X20 25K Credits shared 1 Month access.", 2300, "Video AI"),
        ("ElevenLabs Redeem 300K Credits", "ElevenLabs 300,000 character credits redeem code.", 700, "ElevenLabs"),
        ("ElevenLabs Redeem 1M Credits", "ElevenLabs 1,000,000 character credits redeem code.", 1250, "ElevenLabs"),
        ("ElevenLabs Redeem 3M Credits", "ElevenLabs 3,000,000 character credits redeem code.", 3450, "ElevenLabs"),
        ("HeyGen Creator 1M 28D", "HeyGen Creator 1 Month 28 Days active account.", 2500, "Video AI"),
        ("HeyGen Creator 1M 48H", "HeyGen Creator 1 Month 48 Hours fast access.", 1850, "Video AI"),
        ("Leonardo AI Video Gen", "Leonardo AI Video Generation 1 Month access.", 290, "Video AI"),
        ("iCloud 400GB Your Mail 12M", "Apple iCloud+ 400GB activated on your email 12 Months.", 2400, "Office"),
        ("Wordwall Pro 12M", "Wordwall Pro Teaching Resources 12 Months.", 1950, "Education"),
        ("Wordwall Pro 1M", "Wordwall Pro Teaching Resources 1 Month.", 400, "Education"),

        # Screenshot Page 11
        ("KREA Basic", "KREA AI Basic 1 Month subscription.", 750, "AI Tools"),
        ("KREA Max", "KREA AI Max 1 Month subscription.", 3650, "AI Tools"),
        ("Notion Business 6M", "Notion Business Workspace 6 Months access.", 1750, "Office"),
        ("Quizlet Plus Ultimate 12M", "Quizlet Plus Ultimate 12 Months subscription.", 950, "Education"),
        ("Quizlet Plus 12M", "Quizlet Plus 12 Months individual plan.", 950, "Education"),
        ("Gemini Pro 400GB 12M", "Google Gemini Pro 400GB storage 12 Months.", 600, "Gemini"),
        ("Gemini Pro 5TB 12M", "Google Gemini Pro 5TB storage 12 Months.", 1950, "Gemini"),
        ("Gemini Pro 2TB 12M", "Google Gemini Pro 2TB storage 12 Months.", 900, "Gemini"),
        ("API Kimi 15M Tokens 1D", "Kimi API 15M tokens 1 Day access.", 300, "Kimi"),
        ("API Kimi 100M Tokens 7D", "Kimi API 100M tokens 7 Days access.", 600, "Kimi"),
        ("API Kimi 300M Tokens 30D", "Kimi API 300M tokens 30 Days access.", 1000, "Kimi"),
        ("API Qwen $10 1 Month", "Qwen API $10 balance credits 1 Month.", 220, "Qwen API"),
        ("API Qwen $30 1 Month", "Qwen API $30 balance credits 1 Month.", 290, "Qwen API"),
        ("API Qwen $50 1 Month", "Qwen API $50 balance credits 1 Month.", 400, "Qwen API"),
        ("API Qwen $100 1 Month", "Qwen API $100 balance credits 1 Month.", 650, "Qwen API"),

        # Screenshot Page 12
        ("DeepSeek API $10 1 Month", "DeepSeek API $10 credit balance valid 1 Month.", 220, "DeepSeek AI"),
        ("DeepSeek API $30 1 Month", "DeepSeek API $30 credit balance valid 1 Month.", 290, "DeepSeek AI"),
        ("DeepSeek API $50 1 Month", "DeepSeek API $50 credit balance valid 1 Month.", 410, "DeepSeek AI"),
        ("DeepSeek API $100 1 Month", "DeepSeek API $100 credit balance valid 1 Month.", 610, "DeepSeek AI"),
        ("Cursor Pro API 2600 Credits 1 Month", "Cursor Pro API 2600 Credits 1 Month plan.", 1150, "Cursor API"),
        ("Cursor Pro API 400 Credits/day 1 Month", "Cursor Pro dedicated API key with 400 credits/day refresh.", 1550, "Cursor API"),
        ("Cursor Pro API 6500 Credits 1 Month", "Cursor Pro high-volume 6500 credits 1 month plan.", 1550, "Cursor API"),
        ("Kimi GLM DeepSeek API 50M Tokens 2D", "High speed Kimi GLM DeepSeek API 50M Tokens 2 Days.", 300, "Codex API"),
        ("API QWEN Unlimited Tokens 7D", "Qwen API Unlimited tokens 7-day access.", 500, "AI Tools"),
        ("Kimi GLM DeepSeek API 100M Tokens 4D", "High speed Kimi GLM DeepSeek API 100M Tokens 4-day access.", 650, "Codex API"),
        ("Surfshark VPN 18M", "Surfshark VPN 18 Months unlimited devices.", 700, "VPN"),
        ("Freepik Magnific API Panel 30D", "Freepik Magnific AI upscale & generator panel 30 days.", 500, "AI Tools"),
        ("MiniMax Redeem 300K", "MiniMax 300,000 tokens instant redeem code.", 600, "AI Tools"),
        ("Discord Nitro 2M", "Discord Nitro 2 months subscription gift link.", 900, "Discord"),
        ("Notion Business Admin 1M", "Notion Business Workspace Admin 1 month.", 770, "Office"),

        ("iCloud 2TB 3M", "Apple iCloud+ 2TB Family storage access 3 months.", 1150, "Office"),
        # Screenshot Page 13
        ("MiniMax Redeem 131K", "MiniMax 131,000 character credits instant redeem code.", 260, "AI Tools"),
        ("Flux 03 1M", "Flux 03 AI 1 Month generator access.", 150, "AI Tools"),
        ("ExpressVPN 12M", "ExpressVPN 12 Months full access.", 700, "VPN"),
        ("Canva Education 12M", "Canva Education 12 Months team slot.", 250, "Design"),
        ("Adobe CC Pro 1M", "Adobe Creative Cloud Pro 1 Month plan.", 2253, "Adobe"),
        ("AdGuard VPN 7D", "AdGuard VPN 7 Days private access.", 230, "VPN"),
        ("Surfshark VPN One 7D", "Surfshark VPN One 7 Days security bundle.", 200, "VPN"),
        ("Hotspot Shield VPN 7D", "Hotspot Shield VPN 7 Days access.", 200, "VPN"),
        ("IPVanish VPN 7D", "IPVanish VPN 7 Days access.", 200, "VPN"),
        ("Kaspersky Premium + VPN 1Y", "Kaspersky Premium Security + VPN 1 Year.", 960, "Security"),
        ("ChatGPT Plus 1M Momo Pay", "ChatGPT Plus 1 Month Momo Pay access.", 600, "ChatGPT", 0),
        ("ChatGPT Plus 1M 30D", "ChatGPT Plus 1 Month 30 Days account.", 768, "ChatGPT", 0),
        ("ChatGPT Plus 1M Pix", "ChatGPT Plus 1 Month Pix payment access.", 590, "ChatGPT", 0),
        ("ChatGPT Plus Your Mail 1M", "ChatGPT Plus 1 Month upgraded on your email.", 1800, "ChatGPT", 0),

        # Screenshot Page 14
        ("ChatGPT Business 1M", "ChatGPT Business Workspace 1 Month.", 650, "ChatGPT", 0),
        ("ChatGPT Plus 4M Ready Account", "ChatGPT Plus 4 Months ready-made account.", 5750, "ChatGPT", 0),
        ("Admin Netflix 4K Premium 1M 5 Profiles", "Netflix 4K Premium 1 Month 5 Profiles Admin.", 250, "Streaming", 0),
        ("Admin Netflix 4K 1M", "Netflix 4K Admin 1 Month access.", 380, "Streaming", 0),
        ("ExpressVPN 1 Month Private", "ExpressVPN 1 Month Private dedicated slot.", 350, "VPN", 0),
        ("Codex API 500M Tokens 6D", "Codex API 500M tokens 6 Days access.", 3400, "Codex API", 0),
        ("Apple Music 5M", "Apple Music 5 Months subscription.", 200, "Music", 0),
        ("Apple TV+ 12 Months", "Apple TV+ 12 Months full plan.", 600, "Streaming", 0),
        ("Capcut 6M Personal", "CapCut Pro 6 Months personal account.", 1635, "Design", 0),
        ("Gamma Pro 1 Year", "Gamma Pro AI presentation maker 1 Year.", 2900, "AI Tools", 0),
        ("ElevenLabs Creator 1 Year", "ElevenLabs Creator 1 Year plan.", 3700, "ElevenLabs", 0),
        ("ChatGPT Plus 1M Google Pay", "ChatGPT Plus 1 Month Google Pay.", 870, "ChatGPT", 0),
        ("ChatGPT Plus 1M GPAY", "ChatGPT Plus 1 Month GPAY.", 830, "ChatGPT", 0),
        ("ChatGPT Plus 1M UPI", "ChatGPT Plus 1 Month UPI account.", 600, "ChatGPT", 0),
        ("Kling 66 Credits", "Kling AI 66 Credits pack.", 55, "Video AI", 0),

        # Screenshot Page 15
        ("Gmail account new", "Gmail freshly created PVA account.", 62, "Office", 0),
        ("HBO Max Standard 3 Months", "HBO Max Standard 3 Months private slot.", 200, "Streaming", 0),
        ("HBO Max Standard 5 Months", "HBO Max Standard 5 Months private slot.", 250, "Streaming", 0),
        ("CapCut Pro 7D", "CapCut Pro 7 Days quick access.", 50, "Design", 0),
        ("CapCut Pro 7 Days", "CapCut Pro 7 Days standard license.", 65, "Design", 0),
        ("Adobe Full App Bypass Version 1M", "Adobe Full App Bypass Version 1 Month.", 550, "Adobe", 0),
        ("Loveable Pro 1M Redeem Link", "Loveable Pro 1 Month official redeem link.", 120, "AI Tools", 0),
        ("Windows 10 Pro Retail Key", "Windows 10 Pro Retail digital license key.", 300, "Office", 0),
        ("Duolingo Super 2M", "Duolingo Super 2 Months subscription.", 50, "Education", 0),
        ("Notion Business 1 Year", "Notion Business Workspace 1 Year.", 1250, "Office", 0),
        ("Autodesk Education Plan 1 Year", "Autodesk Education Plan 1 Year access.", 200, "Autodesk", 0),
        ("YouTube Ready Account 3M", "YouTube Premium Ready Account 3 Months.", 220, "Streaming", 0),
        ("JetBrains EDU 1 Year", "JetBrains All Products EDU 1 Year.", 450, "Office", 0),
        ("Higgsfield Starter 1M", "Higgsfield AI Video Starter 1 Month.", 1000, "Video AI", 0),
        ("OpenArt Starter 4K Credits 1M", "OpenArt Starter 4K Credits 1 Month.", 450, "AI Tools", 0),

        # Screenshot Page 16
        ("Coursera Premium 12 Months", "Coursera Plus/Premium 12 Months learning.", 350, "Education", 0),
        ("Spotify Premium 3M", "Spotify Premium 3 Months private account.", 150, "Music", 0),
        ("Kiro Pro Edu Official Owner 1Y", "Kiro Pro EDU Official Owner 1 Year.", 7300, "Education", 0),
        ("Spotify Individual 12M", "Spotify Individual 12 Months subscription.", 2200, "Music", 0),
        ("Spotify Premium 6 Months", "Spotify Premium 6 Months subscription.", 980, "Music", 0),
        ("Claude API 50M Tokens 3 Days", "Claude API 50M tokens valid 3 days.", 600, "Claude", 0),
        ("Apple TV+ 6 Months", "Apple TV+ 6 Months subscription.", 450, "Streaming", 0),
        ("Amazon Prime Video 1 Month", "Amazon Prime Video 1 Month private slot.", 200, "Streaming", 0),
        ("QuillBot Premium 1M", "QuillBot Premium 1 Month paraphrase tool.", 300, "Office", 0),
        ("Snapchat Plus+ 3 Months", "Snapchat Plus 3 Months access.", 500, "Social", 0),
        ("Peacock Official Subscription 1 Year", "Peacock Official TV 1 Year subscription.", 500, "Streaming", 0),
        ("Gamma AI 20K Credits 10 Years", "Gamma AI 20K Credits 10 Years lifetime.", 1100, "AI Tools", 0),
        ("Manus Pro 12 Months", "Manus Pro AI agent 12 Months access.", 3500, "AI Tools", 0),
        ("KlingAI 750-1100 Credits 1M", "KlingAI 750-1100 Credits 1 Month video plan.", 1138, "Video AI", 0),
        ("Kling Random 700-1000 Credits 1M", "Kling Random 700-1000 Credits 1 Month video plan.", 850, "Video AI", 0),

        # Out-of-stock products shown in catalog (Page 17)
        ("Snapchat Plus+ 6 Months", "Snapchat Plus 6 Months subscription access.", 800, "Social", 0),
        ("Cursor Pro + Grok Bot 1M", "Cursor Pro + Grok Bot 1 Month bundle access.", 4500, "AI Tools", 0),
        ("Veo Ultra X20 0 Credits 1M", "Veo Ultra X20 0 Credits 1 Month video generation access.", 1750, "Video AI", 0),
        ("ElevenLabs Creator 1M", "ElevenLabs Creator 1 Month audio subscription.", 750, "ElevenLabs", 0),
        ("Perplexity Pro 1M", "Perplexity Pro 1 Month subscription access.", 800, "AI Tools", 0),
        ("Vidiq Boost 1 Channel 2000 Credits", "Vidiq Boost 1 Channel 2000 Credits 1 Month plan.", 750, "Subscriptions", 0),
        ("Pixverse 600 Credits", "Pixverse 600 Credits 1 Month generation plan.", 100, "Video AI", 0),
        ("Kimi GLM DeepSeek API 500M Tokens 7D", "Kimi GLM DeepSeek API 500M Tokens 7 Days access.", 2400, "Codex API", 0),
        ("CapCut Pro Team 3M", "CapCut Pro Team 3 Months premium access.", 920, "Design", 0),
        ("KlingAI 725-850 Credits 1M", "KlingAI 725-850 Credits 1 Month plan.", 1000, "Video AI", 0),
        ("Seedance 400-600 Credits", "Seedance 400-600 Credits video access.", 150, "AI Tools", 0),
        ("Autodesk All Apps 3Y", "Autodesk All Apps 3 Years student/educator access.", 380, "Autodesk", 0),
        ("Autodesk 1 App 3Y Upgrade", "Autodesk 1 App 3 Years upgrade license.", 500, "Autodesk", 0),
        ("Autodesk Full Apps 3Y Upgrade", "Autodesk Full Apps 3 Years upgrade license.", 800, "Autodesk", 0),
    ]
    for item in MANUAL_CATALOG_SEEDS:
        m_name, m_desc, m_price, m_cat = item[0], item[1], item[2], item[3]
        m_stock = item[4] if len(item) > 4 else None
        exists = db.execute("SELECT id FROM products WHERE name=?", (m_name,)).fetchone()
        if not exists:
            db.execute("""
                INSERT INTO products(name, description, price, category, auto_fulfill, active, supplier_stock, delivery_type)
                VALUES(?,?,?,?,0,1,?,'manual')
            """, (m_name, m_desc, m_price, m_cat, m_stock))
        else:
            if m_stock is not None:
                db.execute("""
                    UPDATE products SET category=?, description=COALESCE(description, ?), price=?, supplier_stock=? WHERE id=?
                """, (m_cat, m_desc, m_price, m_stock, exists["id"]))
            else:
                db.execute("""
                    UPDATE products SET category=?, description=COALESCE(description, ?) WHERE id=?
                """, (m_cat, m_desc, exists["id"]))

    # Custom emojis table for original brand logos (Telegram custom emoji IDs)
    db.execute("""
        CREATE TABLE IF NOT EXISTS custom_emojis(
          key TEXT PRIMARY KEY,
          emoji_id TEXT NOT NULL,
          created TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

migrate_db()


def get_custom_emoji_id(key: str) -> Optional[str]:
    """Retrieve Telegram custom_emoji_id for a brand or UI key from DB or ENV."""
    k = (key or "").strip().lower()
    if not k:
        return None
    try:
        row = db.execute("SELECT emoji_id FROM custom_emojis WHERE key=?", (k,)).fetchone()
        if row and row["emoji_id"]:
            return str(row["emoji_id"]).strip()
    except Exception:
        pass
    env_val = os.environ.get(f"EMOJI_{k.upper()}")
    if env_val:
        return str(env_val).strip()
    return None

dp = Dispatcher()
usd = lambda c: f"${c / 100:.2f}"
esc = html.escape

# In-memory tracking for interactive text inputs (custom quantity, search keyword)
pending_inputs: dict[int, dict] = {}

# --------------------------------------------------------------------------
# Translations
# --------------------------------------------------------------------------
TR = {
    "en": {
        "welcome": "🛍 <b>Welcome to Gemini Pro Store</b>\n\n💵 Best prices\n⚡ Fast delivery after payment\n📦 New products and restocks\n💬 Can't find what you need? Contact support\n\nChoose a section below:",
        "btn_shop": "🛍 Shop",
        "btn_orders": "📦 Order",
        "btn_lang": "🌐 Language",
        "btn_support": "💬 Support",
        "btn_news": "📢 News channel",
        "btn_back": "↩️ Back",
        "btn_refresh": "🔄 Refresh",
        "btn_custom_qty": "✏️ Custom Qty",
        "btn_all_items": "🛍 All items",
        "btn_avail_only": "✅ Available only",
        "btn_show_all": "📦 Show all products",
        "btn_search": "🔍 Search",
        "cat_title": "🛍 <b>Choose a category</b>\nSelect a section or open the full catalog:",
        "prods_title": "🛍 ✨ <b>Choose a product</b>",
        "no_products": "No products available in this section.",
        "btn_buy": "💳 Buy / Checkout",
        "btn_pay": "💳 Pay with Crypto ({amount})",
        "btn_pay_cb": "💳 Pay with @CryptoBot ({amount})",
        "btn_check_pay": "🔄 Check Payment",
        "btn_manual_crypto": "💎 USDT / Binance Pay Details",
        "pay_pending": "⏳ Payment not detected yet. Please complete the invoice in @CryptoBot and check again.",
        "manual_crypto_info": "💳 <b>Manual Crypto Payment · Order #{oid}</b>\n\nItem: <b>{name}</b>\nAmount: <b>{amount}</b>\n\n{details}\n\n<i>Send exact amount and send your transaction hash/receipt to support to verify.</i>",
        "checkout_invoice": "💳 <b>Order #{oid} - Checkout</b>\n\nItem: <b>{name}</b>\nTotal: <b>{amount}</b>\n\nClick the button below to complete payment. Your digital code/link will be delivered automatically upon payment.",
        "purchased": "✅ <b>Purchase Complete!</b>\nOrder #{oid}\n\n<code>{payload}</code>",
        "orders_title": "📦 <b>Your Recent Orders</b>",
        "no_orders": "You haven't made any orders yet. Browse the catalog to get started!",
        "lang_choose": "Choose your language:",
        "lang_set": "Language updated.",
        "stock_auto": "Instant Auto-Delivery",
        "supplier_error": "⚠️ Product is temporarily unavailable from supplier. Please try again shortly or contact support.",
        "checkout_unavailable": "Checkout is temporarily unavailable. Please contact support.",
        "out_of_stock": "Out of stock or unavailable.",
    },
    "es": {
        "welcome": "🛍 <b>Bienvenido a Gemini Pro Store</b>\n\n💵 Mejores precios\n⚡ Entrega rápida tras el pago\n📦 Nuevos productos y reposiciones\n💬 ¿No encuentras lo que buscas? Contacta soporte\n\nElige una sección:",
        "btn_shop": "🛍 Tienda",
        "btn_orders": "📦 Compras",
        "btn_lang": "🌐 Idioma",
        "btn_support": "💬 Soporte",
        "btn_news": "📢 Canal de Noticias",
        "btn_back": "↩️ Volver",
        "btn_refresh": "🔄 Actualizar",
        "btn_custom_qty": "✏️ Cantidad personal",
        "btn_all_items": "🛍 Todos los productos",
        "btn_avail_only": "✅ Solo disponibles",
        "btn_show_all": "📦 Mostrar todos",
        "btn_search": "🔍 Buscar",
        "cat_title": "🛍 <b>Elige una categoría</b>\nSelecciona una sección o abre el catálogo:",
        "prods_title": "🛍 ✨ <b>Elige un producto</b>",
        "no_products": "No hay productos disponibles en esta sección.",
        "btn_buy": "💳 Comprar / Pagar",
        "btn_pay": "💳 Pagar con Crypto ({amount})",
        "btn_pay_cb": "💳 Pagar con @CryptoBot ({amount})",
        "btn_check_pay": "🔄 Verificar Pago",
        "btn_manual_crypto": "💎 Detalles USDT / Binance Pay",
        "pay_pending": "⏳ Pago no detectado aún. Completa el pago en @CryptoBot e intenta de nuevo.",
        "manual_crypto_info": "💳 <b>Pago Manual con Crypto · Pedido #{oid}</b>\n\nArtículo: <b>{name}</b>\nTotal: <b>{amount}</b>\n\n{details}\n\n<i>Envía el monto exacto y tu comprobante/TXID al soporte para recibir tu artículo.</i>",
        "checkout_invoice": "💳 <b>Pedido #{oid} - Pago</b>\n\nArtículo: <b>{name}</b>\nTotal: <b>{amount}</b>\n\nPresiona el botón de abajo para pagar. Tu código/enlace digital se entregará automáticamente tras la confirmación.",
        "purchased": "✅ <b>¡Compra Completada!</b>\nPedido #{oid}\n\n<code>{payload}</code>",
        "orders_title": "📦 <b>Tus Compras Recientes</b>",
        "no_orders": "Aún no has realizado compras. ¡Explora el catálogo para comenzar!",
        "lang_choose": "Elige tu idioma:",
        "lang_set": "Idioma actualizado.",
        "stock_auto": "Entrega Automática Instantánea",
        "supplier_error": "⚠️ El producto no está disponible temporalmente en el proveedor. Intenta más tarde o contacta a soporte.",
        "checkout_unavailable": "El pago no está disponible temporalmente. Contacta a soporte.",
        "out_of_stock": "Agotado o no disponible.",
    },
    "ru": {
        "welcome": "🛍 <b>Добро пожаловать в Gemini Pro Store</b>\n\n💵 Лучшие цены\n⚡ Быстрая выдача после оплаты\n📦 Новые поступления\n💬 Не нашли то, что искали? Напишите в поддержку\n\nВыберите раздел ниже:",
        "btn_shop": "🛍 Магазин",
        "btn_orders": "📦 Покупки",
        "btn_lang": "🌐 Язык",
        "btn_support": "💬 Поддержка",
        "btn_news": "📢 Новости",
        "btn_back": "↩️ Назад",
        "btn_refresh": "🔄 Обновить",
        "btn_custom_qty": "✏️ Своё кол-во",
        "btn_all_items": "🛍 Все товары",
        "btn_avail_only": "✅ Только в наличии",
        "btn_show_all": "📦 Показать все товары",
        "btn_search": "🔍 Поиск",
        "cat_title": "🛍 <b>Выберите категорию</b>\nВыберите раздел или откройте полный каталог:",
        "prods_title": "🛍 ✨ <b>Выберите товар</b>",
        "no_products": "В этом разделе пока нет товаров.",
        "btn_buy": "💳 Купить / Оформить",
        "btn_pay": "💳 Оплатить криптовалютой ({amount})",
        "btn_pay_cb": "💳 Оплатить через @CryptoBot ({amount})",
        "btn_check_pay": "🔄 Проверить оплату",
        "btn_manual_crypto": "💎 Реквизиты USDT / Binance Pay",
        "pay_pending": "⏳ Оплата пока не поступила. Завершите платёж в @CryptoBot и попробуйте снова.",
        "manual_crypto_info": "💳 <b>Оплата криптовалютой · Заказ №{oid}</b>\n\nТовар: <b>{name}</b>\nК оплате: <b>{amount}</b>\n\n{details}\n\n<i>Отправьте точную сумму и подтверждение перевода (TXID) в поддержку для получения товара.</i>",
        "checkout_invoice": "💳 <b>Заказ №{oid} - Оплата</b>\n\nТовар: <b>{name}</b>\nК оплате: <b>{amount}</b>\n\nНажмите кнопку ниже для оплаты. Товар будет отправлен мгновенно после подтверждения платежа.",
        "purchased": "✅ <b>Покупка завершена!</b>\nЗаказ №{oid}\n\n<code>{payload}</code>",
        "orders_title": "📦 <b>Ваши недавние покупки</b>",
        "no_orders": "У вас пока нет покупок. Перейдите в каталог, чтобы сделать заказ!",
        "lang_choose": "Выберите язык:",
        "lang_set": "Язык обновлён.",
        "stock_auto": "Мгновенная автовыдача",
        "supplier_error": "⚠️ Товар временно недоступен у поставщика. Пожалуйста, попробуйте позже или напишите в поддержку.",
        "checkout_unavailable": "Оплата временно недоступна. Напишите в поддержку.",
        "out_of_stock": "Нет в наличии.",
    },
    "ar": {
        "welcome": "🛍 <b>مرحبًا بك في Gemini Pro Store</b>\n\n💵 أفضل الأسعار\n⚡ تسليم سريع بعد الدفع\n📦 منتجات جديدة وتحديثات\n💬 لم تجد ما تبحث عنه؟ تواصل مع الدعم\n\nاختر من القائمة أدناه:",
        "btn_shop": "🛍 المتجر",
        "btn_orders": "📦 المشتريات",
        "btn_lang": "🌐 اللغة",
        "btn_support": "💬 الدعم",
        "btn_news": "📢 قناة الأخبار",
        "btn_back": "↩️ رجوع",
        "btn_refresh": "🔄 تحديث",
        "btn_custom_qty": "✏️ كمية مخصصة",
        "btn_all_items": "🛍 كل المنتجات",
        "btn_avail_only": "✅ المتوفر فقط",
        "btn_show_all": "📦 عرض كل المنتجات",
        "btn_search": "🔍 بحث",
        "cat_title": "🛍 <b>اختر الفئة</b>\nاختر قسمًا أو تصفح الكتالوج بالكامل:",
        "prods_title": "🛍 ✨ <b>اختر المنتج</b>",
        "no_products": "لا توجد منتجات متوفرة في هذا القسم.",
        "btn_buy": "💳 شراء / دفع",
        "btn_pay": "💳 الدفع بالعملات الرقمية ({amount})",
        "btn_pay_cb": "💳 الدفع عبر @CryptoBot ({amount})",
        "btn_check_pay": "🔄 التحقق من الدفع",
        "btn_manual_crypto": "💎 تفاصيل USDT / Binance Pay",
        "pay_pending": "⏳ لم يتم استلام الدفع بعد. يرجى إتمام الدفع في @CryptoBot والمحاولة مرة أخرى.",
        "manual_crypto_info": "💳 <b>الدفع اليدوي بالعملات المشفرة · الطلب #{oid}</b>\n\nالمنتج: <b>{name}</b>\nالمجموع: <b>{amount}</b>\n\n{details}\n\n<i>أرسل المبلغ المحدد مع إشعار/رقم التحويل للدعم الفني لاستلام المنتج.</i>",
        "checkout_invoice": "💳 <b>الطلب #{oid} - إتمام الدفع</b>\n\nالمنتج: <b>{name}</b>\nالمجموع: <b>{amount}</b>\n\nاضغط على الزر أدناه للدفع. سيتم تسليم المنتج الرقمي فورًا بعد التأكيد.",
        "purchased": "✅ <b>تمت عملية الشراء بنجاح!</b>\nالطلب #{oid}\n\n<code>{payload}</code>",
        "orders_title": "📦 <b>مشترياتك الأخيرة</b>",
        "no_orders": "ليس لديك أي مشتريات حتى الآن. تصفح الكتالوج للبدء!",
        "lang_choose": "اختر لغتك:",
        "lang_set": "تم تحديث اللغة.",
        "stock_auto": "تسليم فوري وتلقائي",
        "supplier_error": "⚠️ المنتج غير متوفر مؤقتًا لدى المورد. يرجى المحاولة لاحقًا أو التواصل مع الدعم.",
        "checkout_unavailable": "الدفع غير متاح مؤقتًا. يرجى التواصل مع الدعم.",
        "out_of_stock": "نفدت الكمية أو غير متوفر.",
    },
}
LANG_NAMES = {"en": "English", "es": "Español", "ru": "Русский", "ar": "العربية"}

# --------------------------------------------------------------------------
# Helpers & UI Components
# --------------------------------------------------------------------------
def get_user(uid: int, lang_code: str | None = None):
    row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if row:
        return row
    lang = (lang_code or "en")[:2]
    lang = lang if lang in TR else "en"
    db.execute("INSERT INTO users(id, lang) VALUES(?,?)", (uid, lang))
    return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


def t(uid: int, key: str, **kw) -> str:
    lang = get_user(uid)["lang"]
    return TR.get(lang, TR["en"]).get(key, TR["en"].get(key, key)).format(**kw)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text, data=None, url=None, style=None, emoji_id=None):
    kw = {"text": text}
    if data is not None:
        kw["callback_data"] = data
    if url is not None:
        kw["url"] = url
    if style is not None:
        kw["style"] = style
    if emoji_id:
        kw["icon_custom_emoji_id"] = str(emoji_id)
    return InlineKeyboardButton(**kw)


def stock_count(pid: int) -> int:
    return db.execute("SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0", (pid,)).fetchone()[0]


def is_product_available(p: sqlite3.Row) -> bool:
    """Returns True if the product is active and has available stock."""
    if not p["active"]:
        return False
    is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
    deliv_type = p["delivery_type"] if ("delivery_type" in p.keys() and p["delivery_type"]) else ("auto" if is_auto else "manual")
    sup_stock = p["supplier_stock"] if "supplier_stock" in p.keys() else None
    count = stock_count(p["id"]) if not is_auto else None

    if is_auto and sup_stock is not None and sup_stock <= 0:
        return False
    if sup_stock is not None and sup_stock <= 0:
        return False
    if not is_auto and deliv_type != "manual":
        has_stock_rows = db.execute("SELECT 1 FROM stock WHERE product_id=?", (p["id"],)).fetchone()
        if has_stock_rows and count == 0:
            return False
    return True


async def edit(c: CallbackQuery, text: str, markup=None):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" not in str(e):
            raise
    await c.answer()


async def notify_admins(bot: Bot, text: str):
    for aid in ADMIN_IDS:
        try:
            await bot.send_message(aid, text)
        except Exception:
            pass


def home_kb(uid: int):
    """Home screen keyboard matching Screenshot 2."""
    rows = []

    # Priority 1: Gemini 18 Months ($0.72)
    gemini_p = db.execute(
        "SELECT * FROM products WHERE name LIKE '%Gemini 18 Months%' AND active=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not gemini_p:
        gemini_p = db.execute(
            "SELECT * FROM products WHERE category='Gemini' AND active=1 ORDER BY id LIMIT 1"
        ).fetchone() or db.execute("SELECT * FROM products WHERE active=1 ORDER BY id LIMIT 1").fetchone()

    # Priority 2: Duolingo Super 12 Months ($0.90)
    duo_p = db.execute(
        "SELECT * FROM products WHERE name LIKE '%Duolingo%' AND active=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not duo_p and gemini_p:
        duo_p = db.execute(
            "SELECT * FROM products WHERE active=1 AND id != ? ORDER BY id LIMIT 1", (gemini_p["id"],)
        ).fetchone()

    gemini_emoji_id = get_custom_emoji_id("gemini_product") or get_custom_emoji_id("gemini") or get_custom_emoji_id("google")
    duo_emoji_id = get_custom_emoji_id("duolingo_product") or get_custom_emoji_id("duolingo") or get_custom_emoji_id("education")

    if gemini_p:
        text_g = f"{gemini_p['name']} · {usd(gemini_p['price'])}" if gemini_emoji_id else f"1️⃣ {gemini_p['name']} · {usd(gemini_p['price'])}"
        rows.append([btn(text_g, f"p:{gemini_p['id']}:home", style="success", emoji_id=gemini_emoji_id)])

    if duo_p:
        badge = "🦉" if "duolingo" in duo_p["name"].lower() else "2️⃣"
        text_d = f"{duo_p['name']} · {usd(duo_p['price'])}" if duo_emoji_id else f"{badge} {duo_p['name']} · {usd(duo_p['price'])}"
        rows.append([btn(text_d, f"p:{duo_p['id']}:home", style="success", emoji_id=duo_emoji_id)])

    shop_emoji_id = get_custom_emoji_id("shop")
    orders_emoji_id = get_custom_emoji_id("orders") or get_custom_emoji_id("purchases")
    lang_emoji_id = get_custom_emoji_id("lang") or get_custom_emoji_id("language")
    support_emoji_id = get_custom_emoji_id("support")
    news_emoji_id = get_custom_emoji_id("news") or get_custom_emoji_id("channel")

    rows += [
        [btn(t(uid, "btn_shop"), "shop", style="success", emoji_id=shop_emoji_id),
         btn(t(uid, "btn_orders"), "orders", style="success", emoji_id=orders_emoji_id)],
        [btn(t(uid, "btn_lang"), "lang", style="primary", emoji_id=lang_emoji_id),
         btn(t(uid, "btn_support"), url=SUPPORT_URL, style="primary", emoji_id=support_emoji_id)],
        [btn(t(uid, "btn_news"), url=CHANNEL_URL, style="primary", emoji_id=news_emoji_id)],
    ]
    return kb(rows)


def categories_kb(uid: int):
    """3-column category grid matching Screenshot 3."""
    rows = []
    current_row = []
    for key, name, icon in CATEGORIES:
        custom_id = get_custom_emoji_id(key)
        btn_text = name if custom_id else f"{icon} {name}"
        current_row.append(btn(btn_text, f"cat:{key}:1", style="primary", emoji_id=custom_id))
        if len(current_row) == 3:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    search_id = get_custom_emoji_id("search")
    all_id = get_custom_emoji_id("all_items") or get_custom_emoji_id("all")
    back_id = get_custom_emoji_id("back")
    rows.append([
        btn(t(uid, "btn_search"), "search_start", style="primary", emoji_id=search_id),
        btn(t(uid, "btn_all_items"), "allprods:1", style="primary", emoji_id=all_id)
    ])
    rows.append([btn(t(uid, "btn_back"), "home", style="danger", emoji_id=back_id)])
    return kb(rows)


PAGE_SIZE = 15


def sort_catalog_products(prods: list) -> list:
    """
    Sorts products so that:
    1. In-stock / available goods come first, arranged alphabetically by name (A-Z, case-insensitive).
    2. Out-of-stock goods come on the last pages, arranged alphabetically by name (A-Z, case-insensitive).
    """
    in_stock = []
    out_of_stock = []
    for p in prods:
        if is_product_available(p):
            in_stock.append(p)
        else:
            out_of_stock.append(p)

    in_stock.sort(key=lambda x: str(x["name"]).lower())
    out_of_stock.sort(key=lambda x: str(x["name"]).lower())
    return in_stock + out_of_stock


def render_product_list(uid: int, prods: list, page: int, cb_prefix: str, ctx_prefix: str, back_cb: str, avail_only: int = 0) -> Tuple[str, InlineKeyboardMarkup]:
    """1-column product list with in-stock products arranged alphabetically and out-of-stock goods at the last pages."""
    sorted_prods = sort_catalog_products(prods)
    if avail_only:
        display_prods = [p for p in sorted_prods if is_product_available(p)]
    else:
        display_prods = sorted_prods

    if not display_prods:
        empty_rows = []
        if avail_only:
            empty_rows.append([btn(t(uid, "btn_show_all"), f"{cb_prefix}:1:0", style="primary")])
        empty_rows.append([btn(t(uid, "btn_back"), back_cb, style="danger")])
        return t(uid, "no_products"), kb(empty_rows)

    total_pages = max(1, (len(display_prods) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))

    slice_prods = display_prods[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    rows = []
    for p in slice_prods:
        p_cat = p["category"] if "category" in p.keys() else None
        cat_key = CAT_KEY_BY_NAME.get((p_cat or "").lower()) or CAT_KEY_BY_NAME.get(detect_category(p["name"]).lower())
        p_emoji_id = get_custom_emoji_id(cat_key) if cat_key else None
        icon = get_product_icon(p["name"], p_cat)

        is_avail = is_product_available(p)
        status_mark = "✅" if is_avail else "⛔"
        btn_style = "success" if is_avail else "danger"

        btn_text = f"{p['name']} · {usd(p['price'])} {status_mark}" if p_emoji_id else f"{icon} {p['name']} · {usd(p['price'])} {status_mark}"
        rows.append([btn(btn_text, f"p:{p['id']}:{ctx_prefix}_{avail_only}_{page}", style=btn_style, emoji_id=p_emoji_id)])

    if total_pages > 1:
        pag_row = []
        pag_row.append(btn("◀️", f"{cb_prefix}:{page - 1}:{avail_only}" if page > 1 else "noop", style="primary"))
        pag_row.append(btn(f"{page}/{total_pages}", "noop", style="primary"))
        pag_row.append(btn("▶️", f"{cb_prefix}:{page + 1}:{avail_only}" if page < total_pages else "noop", style="primary"))
        rows.append(pag_row)

    # Filter toggle button
    if avail_only:
        rows.append([btn(t(uid, "btn_show_all"), f"{cb_prefix}:1:0", style="primary")])
    else:
        rows.append([btn(t(uid, "btn_avail_only"), f"{cb_prefix}:1:1", style="success")])

    rows.append([
        btn(t(uid, "btn_refresh"), f"{cb_prefix}:{page}:{avail_only}", style="primary"),
        btn(t(uid, "btn_back"), back_cb, style="danger")
    ])
    return t(uid, "prods_title"), kb(rows)


def render_product_card(uid: int, p: sqlite3.Row, ctx: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Detailed product card and quantity selector matching Screenshot 1."""
    is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
    count = stock_count(p["id"]) if not is_auto else None
    deliv_type = p["delivery_type"] if ("delivery_type" in p.keys() and p["delivery_type"]) else ("auto" if is_auto else "manual")
    sup_stock = p["supplier_stock"] if "supplier_stock" in p.keys() else None

    if is_auto:
        if sup_stock is not None and sup_stock <= 0:
            stock_line = "🔴 <b>Out of stock at supplier</b>"
            has_stock = False
        else:
            stock_info = f" ({sup_stock} pcs)" if sup_stock is not None else ""
            stock_line = f"🟢 ⚡ <b>{t(uid, 'stock_auto')}</b>{stock_info}"
            has_stock = True
    elif count and count > 0:
        stock_line = f"🟢 📦 In stock: {count} pcs"
        has_stock = True
    elif deliv_type == "manual":
        if not p["active"] or (sup_stock is not None and sup_stock <= 0):
            stock_line = "🔴 <b>Out of stock</b>"
            has_stock = False
        else:
            stock_line = "🟡 <b>Manual / Assisted Delivery</b>\n<i>⚡ Credentials or setup link delivered via support after payment</i>"
            has_stock = True
    else:
        stock_line = "🔴 <b>Out of stock</b>"
        has_stock = False

    desc = (p["description"] or "").strip()
    if desc:
        text = (
            f"Item: <b>{esc(p['name'])}</b>\n"
            f"📝 Description: {esc(desc)}\n\n"
            f"💵 Price: {p['price'] / 100:.2f} USD\n"
            f"{stock_line}"
        )
    else:
        text = (
            f"Item: <b>{esc(p['name'])}</b>\n\n"
            f"💵 Price: {p['price'] / 100:.2f} USD\n"
            f"{stock_line}"
        )

    rows = []
    if has_stock:
        qty_row = []
        qty_row.append(btn("1", f"buyqty:{p['id']}:1:{ctx}", style="success"))
        if is_auto:
            qty_row.append(btn("5", f"buyqty:{p['id']}:5:{ctx}", style="success"))
            qty_row.append(btn("10", f"buyqty:{p['id']}:10:{ctx}", style="success"))
        else:
            if count >= 5:
                qty_row.append(btn("5", f"buyqty:{p['id']}:5:{ctx}", style="success"))
                qty_row.append(btn(f"All {count}", f"buyqty:{p['id']}:{count}:{ctx}", style="success"))
            elif count > 1:
                qty_row.append(btn(f"All {count}", f"buyqty:{p['id']}:{count}:{ctx}", style="success"))
        rows.append(qty_row)
        rows.append([btn(t(uid, "btn_custom_qty"), f"cqty:{p['id']}:{ctx}", style="primary")])

    # Resolve back navigation
    if ctx == "home":
        back_cb = "home"
    elif ctx.startswith("c_"):
        parts = ctx.split("_")
        if len(parts) >= 4:
            back_cb = f"cat:{parts[1]}:{parts[3]}:{parts[2]}"
        elif len(parts) == 3:
            back_cb = f"cat:{parts[1]}:{parts[2]}:0"
        else:
            back_cb = "shop"
    elif ctx.startswith("all_"):
        parts = ctx.split("_")
        if len(parts) >= 3:
            back_cb = f"allprods:{parts[2]}:{parts[1]}"
        elif len(parts) == 2:
            back_cb = f"allprods:{parts[1]}:0"
        else:
            back_cb = "shop"
    elif ctx.startswith("s_"):
        parts = ctx.split("_")
        if len(parts) >= 4:
            back_cb = f"srch:{parts[1]}:{parts[3]}:{parts[2]}"
        elif len(parts) == 3:
            back_cb = f"srch:{parts[1]}:{parts[2]}:0"
        else:
            back_cb = "shop"
    else:
        back_cb = "shop"

    rows.append([
        btn(t(uid, "btn_refresh"), f"p:{p['id']}:{ctx}", style="primary"),
        btn(t(uid, "btn_back"), back_cb, style="danger")
    ])
    return text, kb(rows)


# --------------------------------------------------------------------------
# Order Fulfillment Logic (with Quantity Support)
# --------------------------------------------------------------------------
async def fulfill_order(bot: Bot, oid: int) -> Tuple[Optional[str], Optional[str]]:
    """
    Fulfills an order automatically:
      - If supplier catalog item: calls Reseller API POST /orders with quantity, saves order.value.
      - If manual stock: pulls available stock items from database for specified quantity.
    Returns (payload, error_key).
    """
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return None, "out_of_stock"
    if o["payload"] and o["status"] == "completed":
        return o["payload"], None

    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (o["product_id"],)).fetchone()
    if not p:
        return None, "out_of_stock"

    qty = o["quantity"] if ("quantity" in o.keys() and o["quantity"]) else 1

    # Option A: Supplier Catalog Auto-fulfillment
    if p["auto_fulfill"] and p["supplier_product_id"]:
        sup_name = p["supplier"] if ("supplier" in p.keys() and p["supplier"]) else "default"
        sup_client = supplier_registry.get(sup_name) or reseller
        try:
            order_res = await sup_client.create_order(product_id=p["supplier_product_id"], quantity=qty)
            payload = order_res.get("value")
            supplier_order_id = order_res.get("order_code")
            if not payload:
                raise ResellerAPIError(f"Supplier order succeeded but returned empty value: {order_res}")
        except Exception as e:
            log.exception("Supplier order failed for order %s (supplier: %s)", oid, sup_name)
            db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
            await notify_admins(bot, f"⚠️ Supplier order failed for order #{oid} (user {o['user_id']}, product #{p['id']}, supplier '{sup_name}', qty {qty}):\n<code>{esc(str(e))}</code>")
            return None, "supplier_error"

        db.execute(
            "UPDATE orders SET payload=?, supplier_order_id=?, supplier=?, status='completed' WHERE id=?",
            (payload, supplier_order_id, sup_name, oid)
        )
        return payload, None

    # Option B: Local Stock Items
    db.execute("BEGIN IMMEDIATE")
    try:
        s_items = db.execute(
            "SELECT * FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT ?",
            (p["id"], qty)
        ).fetchall()
        if len(s_items) >= qty:
            ids = [item["id"] for item in s_items]
            db.execute(f"UPDATE stock SET sold=1 WHERE id IN ({','.join(['?']*len(ids))})", ids)
            payload = "\n".join(item["payload"] for item in s_items)
            db.execute("UPDATE orders SET payload=?, status='completed' WHERE id=?", (payload, oid))
            db.execute("COMMIT")
            return payload, None
        else:
            db.execute("ROLLBACK")
    except Exception:
        db.execute("ROLLBACK")
        raise

    # Option C: Manual / Assisted Fulfillment
    deliv = p["delivery_type"] if ("delivery_type" in p.keys() and p["delivery_type"]) else "auto"
    if deliv == "manual" or not p["auto_fulfill"]:
        db.execute("UPDATE orders SET status='manual_pending' WHERE id=?", (oid,))
        await notify_admins(
            bot,
            f"🔔 <b>New Manual Order #{oid}</b>\n"
            f"• Item: <b>{esc(p['name'])}</b> (Qty: {qty})\n"
            f"• User ID: <code>{o['user_id']}</code>\n"
            f"• Amount: <b>{usd(o['price'])}</b>\n\n"
            f"👉 Use <code>/deliver {oid} &lt;credentials/link&gt;</code> to send item to user."
        )
        return "MANUAL_PENDING", None

    db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
    return None, "out_of_stock"


# --------------------------------------------------------------------------
# User Handlers & Navigation
# --------------------------------------------------------------------------
@dp.message(CommandStart())
@dp.message(Command("start"))
@dp.message(Command("menu"))
async def start(m: Message):
    pending_inputs.pop(m.from_user.id, None)
    get_user(m.from_user.id, m.from_user.language_code)
    await m.answer(t(m.from_user.id, "welcome"), reply_markup=home_kb(m.from_user.id))


@dp.message(Command("shop"))
async def cmd_shop(m: Message):
    pending_inputs.pop(m.from_user.id, None)
    uid = m.from_user.id
    get_user(uid, m.from_user.language_code)
    await m.answer(t(uid, "cat_title"), reply_markup=categories_kb(uid))


@dp.callback_query(F.data == "home")
async def home(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    await edit(c, t(uid, "welcome"), home_kb(uid))


@dp.callback_query(F.data == "shop")
async def shop(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    await edit(c, t(uid, "cat_title"), categories_kb(uid))


@dp.callback_query(F.data.startswith("cat:"))
async def on_category(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    parts = c.data.split(":")
    key = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 1
    avail_only = int(parts[3]) if len(parts) > 3 else 0
    prods = get_products_for_category(key)
    text, markup = render_product_list(
        uid, prods, page=page,
        cb_prefix=f"cat:{key}",
        ctx_prefix=f"c_{key}",
        back_cb="shop",
        avail_only=avail_only
    )
    await edit(c, text, markup)


@dp.callback_query(F.data.startswith("allprods:"))
async def on_all_prods(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    parts = c.data.split(":")
    page = int(parts[1]) if len(parts) > 1 else 1
    avail_only = int(parts[2]) if len(parts) > 2 else 0
    prods = db.execute("SELECT * FROM products ORDER BY id").fetchall()
    text, markup = render_product_list(
        uid, prods, page=page,
        cb_prefix="allprods",
        ctx_prefix="all",
        back_cb="shop",
        avail_only=avail_only
    )
    await edit(c, text, markup)


@dp.callback_query(F.data == "search_start")
async def on_search_start(c: CallbackQuery):
    uid = c.from_user.id
    pending_inputs[uid] = {"action": "search"}
    await c.message.answer(
        "🔍 <b>Search Products:</b>\n\n"
        "Please type the name or keyword of the product you are looking for.\n"
        "Send /cancel to return to categories."
    )
    await c.answer()


@dp.callback_query(F.data.startswith("srch:"))
async def on_search_page(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    parts = c.data.split(":")
    term = parts[1]
    page = int(parts[2]) if len(parts) > 2 else 1
    avail_only = int(parts[3]) if len(parts) > 3 else 0
    prods = db.execute(
        "SELECT * FROM products WHERE (LOWER(name) LIKE ? OR LOWER(description) LIKE ?) ORDER BY id",
        (f"%{term.lower()}%", f"%{term.lower()}%")
    ).fetchall()
    text, markup = render_product_list(
        uid, prods, page=page,
        cb_prefix=f"srch:{term}",
        ctx_prefix=f"s_{term}",
        back_cb="shop",
        avail_only=avail_only
    )
    await edit(c, text, markup)


@dp.callback_query(F.data == "noop")
async def on_noop(c: CallbackQuery):
    await c.answer()


@dp.callback_query(F.data.startswith("p:"))
async def on_product(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    parts = c.data.split(":", 2)
    pid = int(parts[1])
    ctx = parts[2] if len(parts) > 2 else "shop"
    p = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
    if not p:
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)
    text, markup = render_product_card(uid, p, ctx)
    await edit(c, text, markup)


def manual_crypto_details() -> str:
    lines = []
    if BINANCE_PAY_ID and not BINANCE_PAY_ID.startswith("your_"):
        lines.append(f"• <b>Binance Pay ID:</b> <code>{esc(BINANCE_PAY_ID)}</code>")
    if CRYPTO_USDT_BEP20 and not CRYPTO_USDT_BEP20.startswith("0xYour"):
        lines.append(f"• <b>USDT (BEP-20 / BSC):</b>\n<code>{esc(CRYPTO_USDT_BEP20)}</code>")
    if CRYPTO_USDT_TRC20 and not CRYPTO_USDT_TRC20.startswith("TYour"):
        lines.append(f"• <b>USDT (TRC-20 / TRON):</b>\n<code>{esc(CRYPTO_USDT_TRC20)}</code>")
    return "\n".join(lines) if lines else "<i>Contact support for payment addresses.</i>"


def has_manual_crypto() -> bool:
    return bool(
        (BINANCE_PAY_ID and not BINANCE_PAY_ID.startswith("your_")) or
        (CRYPTO_USDT_BEP20 and not CRYPTO_USDT_BEP20.startswith("0xYour")) or
        (CRYPTO_USDT_TRC20 and not CRYPTO_USDT_TRC20.startswith("TYour"))
    )


async def create_cryptobot_invoice(order_id: int, item_name: str, cents: int) -> dict:
    """Create a @CryptoBot invoice for this specific product order."""
    payload = {
        "currency_type": "fiat",
        "fiat": "USD",
        "amount": f"{cents / 100:.2f}",
        "description": f"Order #{order_id}: {item_name}",
        "payload": str(order_id),
    }
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post(
            "https://pay.crypt.bot/api/createInvoice",
            json=payload,
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        ) as r:
            data = await r.json()
            if not data.get("ok"):
                raise RuntimeError(f"CryptoBot error: {data}")
            return data["result"]


async def check_cryptobot_invoice(invoice_id: int) -> bool:
    """Check if a @CryptoBot invoice is paid."""
    timeout = aiohttp.ClientTimeout(total=15)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.get(
            f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}",
            headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
        ) as r:
            data = await r.json()
            if data.get("ok") and data.get("result", {}).get("items"):
                items = data["result"]["items"]
                return items[0].get("status") == "paid"
            return False


def render_order_summary(uid: int, p: sqlite3.Row, qty: int, ctx: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Order Summary matching Screenshot 2."""
    total_cents = p["price"] * qty
    icon = get_product_icon(p["name"], p["category"] if "category" in p.keys() else None)
    unit_usd = f"{p['price'] / 100:.2f}"
    total_usd = f"{total_cents / 100:.2f}"

    text = (
        "🧾 <b>Order Summary</b>\n\n"
        f"🥇 {icon} <b>{esc(p['name'])}</b>\n"
        f"🔢 <b>Qty:</b> {qty}\n"
        f"🤑 <b>Price:</b> ${unit_usd} each\n"
        f"🪙 <b>Total:</b> ${total_usd} USDT\n\n"
        "💡 <b>Buy more & save:</b>\n"
        "• 10+ redeem links — save $0.02 each\n"
        "• 20+ redeem links — save $0.04 each\n"
        "• 50+ redeem links — save $0.05 each\n\n"
        "<b>Choose how to pay:</b>"
    )
    rows = [
        [btn("💳 Pay Directly", f"pay_dir:{p['id']}:{qty}:{ctx}", style="success")],
        [
            btn("◀️ Back", f"p:{p['id']}:{ctx}", style="primary"),
            btn("❌ Cancel", "home", style="danger")
        ]
    ]
    return text, kb(rows)


def render_payment_methods(uid: int, p: sqlite3.Row, qty: int, ctx: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Payment method selection screen matching Screenshot 3."""
    rows = [
        [btn("🟡 Binance Pay", f"pay_opt:binance:{p['id']}:{qty}:{ctx}", style="primary")],
        [btn("🟠 USDT BEP20", f"pay_opt:bep20:{p['id']}:{qty}:{ctx}", style="primary")],
        [btn("🔴 USDT TRC20", f"pay_opt:trc20:{p['id']}:{qty}:{ctx}", style="primary")],
        [btn("🤖 CryptoBot", f"pay_opt:cryptobot:{p['id']}:{qty}:{ctx}", style="primary")],
        [btn("◀️ Back", f"summary:{p['id']}:{qty}:{ctx}", style="primary")]
    ]
    return "<b>Choose how to pay:</b>", kb(rows)


async def start_checkout(event: Message | CallbackQuery, uid: int, p: sqlite3.Row, qty: int, ctx: str):
    """Entrypoint to order summary screen."""
    pid = p["id"]
    is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
    if not is_auto:
        avail = stock_count(pid)
        if avail < qty:
            msg = f"⚠️ Not enough stock available ({avail} left)."
            if isinstance(event, CallbackQuery):
                return await event.answer(msg, show_alert=True)
            return await event.answer(msg)

    text, markup = render_order_summary(uid, p, qty, ctx)
    if isinstance(event, CallbackQuery):
        await edit(event, text, markup)
    else:
        await event.answer(text, reply_markup=markup)


@dp.callback_query(F.data.startswith("summary:"))
async def on_summary_back(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    _, pid_str, qty_str, ctx = c.data.split(":", 3)
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (int(pid_str),)).fetchone()
    if not p:
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)
    text, markup = render_order_summary(uid, p, int(qty_str), ctx)
    await edit(c, text, markup)


@dp.callback_query(F.data.startswith("pay_dir:"))
async def on_pay_directly(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    _, pid_str, qty_str, ctx = c.data.split(":", 3)
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (int(pid_str),)).fetchone()
    if not p:
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)
    text, markup = render_payment_methods(uid, p, int(qty_str), ctx)
    await edit(c, text, markup)


@dp.callback_query(F.data.startswith("pay_opt:"))
async def on_pay_option_selected(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    _, method, pid_str, qty_str, ctx = c.data.split(":", 4)
    pid, qty = int(pid_str), int(qty_str)
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)

    is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
    if not is_auto:
        avail = stock_count(pid)
        if avail < qty:
            return await c.answer(f"⚠️ Not enough stock available ({avail} left).", show_alert=True)

    total_cents = p["price"] * qty
    total_usdt = f"{total_cents / 100:.2f}"
    icon = get_product_icon(p["name"], p["category"] if "category" in p.keys() else None)

    # Create pending order in DB
    cur = db.execute(
        "INSERT INTO orders(user_id, product_id, product_name, price, quantity, payment_method, status) VALUES(?,?,?,?,?,?,'pending')",
        (uid, pid, p["name"], total_cents, qty, method)
    )
    oid = cur.lastrowid

    # 1. CryptoBot Gateway (Screenshot 1)
    if method == "cryptobot":
        pay_url = None
        inv_id = None
        if CRYPTOBOT_TOKEN and not CRYPTOBOT_TOKEN.startswith("token-from"):
            try:
                inv = await create_cryptobot_invoice(oid, f"{p['name']} x{qty}", total_cents)
                inv_id = inv["invoice_id"]
                db.execute("UPDATE orders SET invoice_id=? WHERE id=?", (str(inv_id), oid))
                pay_url = inv.get("bot_invoice_url") or inv.get("pay_url")
            except Exception:
                log.exception("CryptoBot invoice creation failed")

        if not pay_url:
            pay_url = f"https://t.me/CryptoBot?start=pay_{oid}"
            inv_id = inv_id or oid

        text = (
            "<u>🤖 <b>CryptoBot Checkout</b></u>\n"
            "────────────────────────\n\n"
            f"🥇 <b>Product:</b> {icon} <b>{esc(p['name'])}</b>\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            f"➡️ <b>Total to pay:</b> <b>${total_usdt} USDT</b>\n"
            "────────────────────────\n\n"
            f"1. Tap <b>Pay ${total_usdt} USDT</b> below to open CryptoBot.\n"
            "2. Complete payment in Telegram.\n"
            "3. Return and tap <b>I've Paid — Check Status</b> for instant delivery."
        )
        rows = [
            [btn(f"💳 Pay ${total_usdt} USDT", url=pay_url, style="success")],
            [btn("✅ I've Paid — Check Status", f"chk_cb:{oid}:{inv_id}", style="success")],
            [btn("◀️ Back", f"pay_dir:{pid}:{qty}:{ctx}", style="primary")],
            [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
        ]
        return await edit(c, text, kb(rows))

    # 2. Binance Pay (Screenshot 1)
    if method == "binance":
        text = (
            "<u>🟡 <b>Binance Pay</b></u>\n"
            "────────────────────────\n"
            f"🥇 <b>Product:</b> {icon} <b>{esc(p['name'])}</b>\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            "➡️ <b>Send exactly this amount:</b>\n"
            f"<code>{total_usdt} USDT</code>\n"
            "👆 <i>Tap to copy.</i>\n"
            "────────────────────────\n"
            "🏦 <b>Binance Pay ID:</b>\n"
            f"<code>{BINANCE_PAY_ID}</code>\n"
            "👆 <i>Tap to copy — always copy it from this message.</i>\n\n"
            "⚠️ <b>Send only USDT via Binance Pay to the Pay ID above.</b>\n"
            "────────────────────────\n"
            f"💸 <b>Step 1:</b> Open Binance App → Pay → Send to Binance Pay ID: <code>{BINANCE_PAY_ID}</code>.\n"
            "🔗 <b>Step 2:</b> Copy your <b>Binance Pay Order ID / Transaction ID</b>.\n"
            "✅ <b>Step 3:</b> Tap <b>Submit Transaction ID</b> below and paste it in the chat.\n\n"
            "⌛ <b>Please pay within 70 minutes</b>, otherwise this order expires and needs manual review.\n\n"
            "<i>Delivery is instant once verified by admin.</i>"
        )
        rows = [
            [btn("🔗 Submit Transaction ID", f"submit_tx:{oid}", style="primary")],
            [btn("✅ I've Paid — Check Status", f"chk_manual:{oid}", style="success")],
            [btn("◀️ Back", f"pay_dir:{pid}:{qty}:{ctx}", style="primary")],
            [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
        ]
        return await edit(c, text, kb(rows))

    # Calculate unique decimal amount for on-chain USDT payments (extra decimals = unique order ID)
    extra_suffix = f"{(oid % 90) + 10:02d}"
    exact_crypto_amount = f"{total_cents / 100:.2f}{extra_suffix}"
    db.execute("UPDATE orders SET expected_amount=? WHERE id=?", (f"{exact_crypto_amount} USDT", oid))

    # 3. USDT BEP20 (Screenshot 2)
    if method == "bep20":
        text = (
            "<u>🟠 <b>USDT BEP20 (BEP20)</b></u>\n"
            "────────────────────────\n"
            f"🥇 <b>Product:</b> {icon} <b>{esc(p['name'])}</b>\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            "➡️ <b>Send exactly this amount:</b>\n"
            f"<code>{exact_crypto_amount} USDT</code>\n"
            "👆 <i>Tap to copy — the extra decimals are your unique order ID. Sending a different amount will NOT verify.</i>\n"
            "────────────────────────\n"
            "🏦 <b>Deposit Address (BEP20):</b>\n"
            f"<code>{CRYPTO_USDT_BEP20}</code>\n"
            "👆 <i>Tap to copy — always copy it from this message.</i>\n\n"
            "⚠️ <b>Send only USDT on BEP20. Wrong network = lost funds.</b>\n"
            "🚫 <b>Send only to the address above. Payments sent to an exchange deposit address or to an older address from a previous chat cannot be verified automatically.</b>\n"
            "────────────────────────\n"
            f"💸 <b>Step 1:</b> Send exactly {exact_crypto_amount} USDT to the address above.\n"
            "🔗 <b>Step 2:</b> Copy your <b>Transaction ID (TxID / Tx Hash)</b> from your wallet's transaction history.\n"
            "✅ <b>Step 3:</b> Tap <b>Submit Transaction ID</b> below and paste it in the chat.\n\n"
            f"❌ <b>Do not round the amount (e.g. {total_usdt}) — rounded payments cannot be verified automatically.</b>\n"
            "⌛ <b>Please pay within 70 minutes</b>, otherwise this order expires and needs manual review.\n\n"
            "<i>Delivery is instant once the transaction is confirmed on-chain (usually under a minute).</i>"
        )
        rows = [
            [btn("🔗 Submit Transaction ID", f"submit_tx:{oid}", style="primary")],
            [btn("✅ I've Paid — Check Status", f"chk_manual:{oid}", style="success")],
            [btn("◀️ Back", f"pay_dir:{pid}:{qty}:{ctx}", style="primary")],
            [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
        ]
        return await edit(c, text, kb(rows))

    # 4. USDT TRC20 (Screenshot 3)
    if method == "trc20":
        text = (
            "<u>🔴 <b>USDT TRC20 (TRC20)</b></u>\n"
            "────────────────────────\n"
            f"🥇 <b>Product:</b> {icon} <b>{esc(p['name'])}</b>\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            "➡️ <b>Send exactly this amount:</b>\n"
            f"<code>{exact_crypto_amount} USDT</code>\n"
            "👆 <i>Tap to copy — the extra decimals are your unique order ID.</i>\n"
            "────────────────────────\n"
            "🏦 <b>Deposit Address (TRC20):</b>\n"
            f"<code>{CRYPTO_USDT_TRC20}</code>\n"
            "👆 <i>Tap to copy — always copy it from this message.</i>\n\n"
            "⚠️ <b>Send only USDT on TRC20 (Tron). Wrong network = lost funds.</b>\n"
            "────────────────────────\n"
            f"💸 <b>Step 1:</b> Send exactly {exact_crypto_amount} USDT to the address above.\n"
            "🔗 <b>Step 2:</b> Copy your <b>Transaction ID (TxID / Tx Hash)</b>.\n"
            "✅ <b>Step 3:</b> Tap <b>Submit Transaction ID</b> below and paste it in the chat.\n\n"
            "⌛ <b>Please pay within 70 minutes</b>, otherwise this order expires and needs manual review.\n\n"
            "<i>Delivery is instant once the transaction is confirmed on-chain (usually under a minute).</i>"
        )
        rows = [
            [btn("🔗 Submit Transaction ID", f"submit_tx:{oid}", style="primary")],
            [btn("✅ I've Paid — Check Status", f"chk_manual:{oid}", style="success")],
            [btn("◀️ Back", f"pay_dir:{pid}:{qty}:{ctx}", style="primary")],
            [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
        ]
        return await edit(c, text, kb(rows))


@dp.callback_query(F.data.startswith("submit_tx:"))
async def on_submit_tx(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    uid = c.from_user.id
    pending_inputs[uid] = {"action": "txid", "oid": oid}
    await c.message.answer(
        f"✍️ <b>Submit Transaction ID for Order #{oid}:</b>\n\n"
        f"Please paste your <b>Transaction ID / Order ID / Hash</b> in this chat.\n"
        f"Our admins will verify your payment and release your items immediately.\n\n"
        f"Send /cancel to return to the order."
    )
    await c.answer()


async def check_and_fulfill_onchain(oid: int, txid: str, uid: int, bot_instance: Bot) -> Tuple[bool, str]:
    """
    Attempts on-chain verification for USDT BEP20 or TRC20 transactions.
    If valid, marks order paid and triggers fulfillment.
    Returns (verified: bool, status_message: str).
    """
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return False, "Order not found."
    if o["status"] == "completed" and o["payload"]:
        return True, "Order already completed."

    # Prevent replay attack: check if this txid was already used for a completed order
    dup = db.execute(
        "SELECT id FROM orders WHERE txid=? AND status='completed' AND id != ?",
        (txid, oid)
    ).fetchone()
    if dup:
        return False, f"⚠️ This Transaction ID was already claimed for Order #{dup['id']}."

    method = (o["payment_method"] or "").lower()
    total_cents = o["price"]
    required_usd = total_cents / 100.0

    verified = False
    details = ""
    detected_amount = None

    if "bep20" in method:
        if not CRYPTO_USDT_BEP20 or not CRYPTO_USDT_BEP20.startswith("0x"):
            return False, "BEP-20 wallet address is not configured."
        verified, details, detected_amount = await onchain.verify_bep20_usdt(
            tx_hash=txid,
            expected_recipient=CRYPTO_USDT_BEP20,
            expected_usd=required_usd
        )
    elif "trc20" in method:
        if not CRYPTO_USDT_TRC20:
            return False, "TRC-20 wallet address is not configured."
        verified, details, detected_amount = await onchain.verify_trc20_usdt(
            tx_hash=txid,
            expected_recipient=CRYPTO_USDT_TRC20,
            expected_usd=required_usd
        )
    else:
        return False, "This payment method requires manual confirmation."

    if verified:
        db.execute("UPDATE orders SET txid=?, status='paid' WHERE id=?", (txid, oid))
        payload, err = await fulfill_order(bot_instance, oid)
        if err:
            log.error("Failed to fulfill auto-verified order %s: %s", oid, err)
            await notify_admins(
                bot_instance,
                f"⚠️ Payment verified on-chain for Order #{oid}, but auto-fulfillment failed: {err}\n"
                f"• Method: {esc(method.upper())}\n"
                f"• TxID: <code>{esc(txid)}</code>\n"
                f"👉 Use <code>/deliver {oid} &lt;item&gt;</code> to deliver manually."
            )
            return True, "Payment verified on-chain! Our support team is completing your delivery shortly."

        user_mention = f"User {uid}"
        await notify_admins(
            bot_instance,
            f"⚡ <b>Auto-Verified & Delivered!</b>\n"
            f"• <b>Order:</b> #{oid}\n"
            f"• <b>Product:</b> {esc(o['product_name'])}\n"
            f"• <b>Amount:</b> ${detected_amount:.2f} USDT (Required: {usd(o['price'])})\n"
            f"• <b>Method:</b> {esc(method.upper())}\n"
            f"• <b>TxID:</b> <code>{esc(txid)}</code>"
        )
        return True, "VERIFIED_AND_DELIVERED"

    return False, details


@dp.callback_query(F.data.startswith("chk_manual:"))
async def on_check_manual_status(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    uid = c.from_user.id
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await c.answer("Order not found.", show_alert=True)
    if o["status"] == "completed" and o["payload"]:
        await edit(c, t(uid, "purchased", oid=oid, payload=esc(o["payload"])), kb([[btn(t(uid, "btn_back"), "home", style="primary")]]))
        return await c.answer("✅ Order completed and delivered!", show_alert=True)

    if not o["txid"]:
        return await c.answer("⚠️ Please tap 'Submit Transaction ID' first so we can verify your payment.", show_alert=True)

    method = (o["payment_method"] or "").lower()
    if "bep20" in method or "trc20" in method:
        verified, result = await check_and_fulfill_onchain(oid, o["txid"], uid, c.bot)
        if verified and result == "VERIFIED_AND_DELIVERED":
            comp_o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if comp_o and comp_o["payload"] and comp_o["payload"] != "MANUAL_PENDING":
                await edit(c, t(uid, "purchased", oid=oid, payload=esc(comp_o["payload"])), kb([[btn(t(uid, "btn_back"), "home", style="primary")]]))
                return await c.answer("✅ Verified on blockchain and delivered!", show_alert=True)

    return await c.answer("⏳ Your payment is currently under verification. You will receive your item automatically once confirmed!", show_alert=True)


@dp.callback_query(F.data.startswith("cancel_order:"))
async def on_cancel_order(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    db.execute("UPDATE orders SET status='cancelled' WHERE id=? AND status='pending'", (oid,))
    await c.answer("❌ Order cancelled.", show_alert=True)
    uid = c.from_user.id
    await edit(c, t(uid, "welcome"), home_kb(uid))


@dp.callback_query(F.data.startswith("buyqty:"))
async def on_buy_qty(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    _, pid_str, qty_str, ctx = c.data.split(":", 3)
    pid, qty = int(pid_str), int(qty_str)
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p or not is_product_available(p):
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)
    await start_checkout(c, uid, p, qty, ctx)


@dp.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    """Backward compatibility for single quantity checkout."""
    pending_inputs.pop(c.from_user.id, None)
    uid, pid = c.from_user.id, int(c.data[4:])
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p or not is_product_available(p):
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)
    await start_checkout(c, uid, p, qty=1, ctx="shop")


@dp.callback_query(F.data.startswith("cqty:"))
async def on_custom_qty(c: CallbackQuery):
    uid = c.from_user.id
    parts = c.data.split(":", 2)
    pid = int(parts[1])
    ctx = parts[2] if len(parts) > 2 else "shop"
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p or not is_product_available(p):
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)

    pending_inputs[uid] = {"action": "cqty", "pid": pid, "ctx": ctx}
    is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
    count = stock_count(pid) if not is_auto else 999

    await c.message.answer(
        f"✏️ <b>Enter Custom Quantity for {esc(p['name'])}:</b>\n\n"
        f"Please send the quantity you want to purchase (1 to {count} pcs).\n"
        f"Send /cancel to return to the product card."
    )
    await c.answer()


@dp.message(Command("cancel"))
async def handle_cancel(m: Message):
    uid = m.from_user.id
    state = pending_inputs.pop(uid, None)
    if not state:
        return await m.answer(t(uid, "welcome"), reply_markup=home_kb(uid))
    action = state.get("action")
    if action == "cqty":
        pid = state["pid"]
        ctx = state.get("ctx", "shop")
        p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if p:
            text, markup = render_product_card(uid, p, ctx)
            return await m.answer(text, reply_markup=markup)
    await m.answer(t(uid, "welcome"), reply_markup=home_kb(uid))


@dp.message(F.text, ~F.text.startswith("/"))
async def handle_user_text_input(m: Message):
    uid = m.from_user.id
    if uid not in pending_inputs:
        return
    state = pending_inputs.pop(uid)
    action = state.get("action")

    if action == "cqty":
        pid = state["pid"]
        ctx = state.get("ctx", "shop")
        p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
        if not p or not is_product_available(p):
            return await m.answer(t(uid, "out_of_stock"), reply_markup=kb([[btn(t(uid, "btn_back"), "shop", style="danger")]]))

        try:
            qty = int(m.text.strip())
            if qty <= 0:
                raise ValueError
        except ValueError:
            pending_inputs[uid] = state
            return await m.answer("⚠️ Please enter a valid positive number (e.g. 1, 2, 5) or send /cancel:")

        is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
        if not is_auto:
            avail = stock_count(pid)
            if qty > avail:
                pending_inputs[uid] = state
                return await m.answer(f"⚠️ Only {avail} in stock. Please enter a quantity up to {avail} (or send /cancel):")

        await start_checkout(m, uid, p, qty, ctx)

    elif action == "search":
        term = m.text.strip()
        prods = db.execute(
            "SELECT * FROM products WHERE LOWER(name) LIKE ? OR LOWER(description) LIKE ? ORDER BY id",
            (f"%{term.lower()}%", f"%{term.lower()}%")
        ).fetchall()
        text, markup = render_product_list(
            uid, prods, page=1,
            cb_prefix=f"srch:{term}",
            ctx_prefix=f"s_{term}",
            back_cb="shop"
        )
        await m.answer(text, reply_markup=markup)

    elif action == "txid":
        oid = state["oid"]
        txid = m.text.strip()
        o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return await m.answer("⚠️ Order not found.")

        db.execute("UPDATE orders SET txid=? WHERE id=?", (txid, oid))
        method = (o["payment_method"] or "").lower()

        # Attempt instant on-chain verification for USDT BEP20 or TRC20
        if "bep20" in method or "trc20" in method:
            verif_msg = await m.answer("🔍 <i>Verifying transaction on the blockchain...</i>")
            verified, result = await check_and_fulfill_onchain(oid, txid, uid, m.bot)
            try:
                await verif_msg.delete()
            except Exception:
                pass

            if verified:
                if result == "VERIFIED_AND_DELIVERED":
                    comp_o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
                    if comp_o and comp_o["payload"] == "MANUAL_PENDING":
                        return await m.answer(
                            f"🎉 <b>Payment Verified On-Chain for Order #{oid}!</b>\n\n"
                            f"Item: <b>{esc(o['product_name'])}</b>\n\n"
                            f"🟡 <i>This product is an assisted/manual setup service. Our support team has received your order and will deliver your credentials or setup link directly to you here shortly!</i>\n\n"
                            f"If you have any questions, contact Support: {SUPPORT_URL}"
                        )
                    elif comp_o and comp_o["payload"]:
                        return await m.answer(t(uid, "purchased", oid=oid, payload=esc(comp_o["payload"])))
                else:
                    return await m.answer(f"✅ {result}")

        # Fallback to admin review (e.g. for Binance Pay or if block confirmation is still pending)
        await m.answer(
            f"✅ <b>Transaction ID Received!</b>\n\n"
            f"• <b>Order:</b> #{oid}\n"
            f"• <b>Reference:</b> <code>{esc(txid)}</code>\n\n"
            f"Our team is verifying your payment. Your product will be delivered here automatically once confirmed!"
        )
        user_mention = f"@{m.from_user.username}" if m.from_user.username else f"User {uid}"
        await notify_admins(
            m.bot,
            f"🔔 <b>New Manual Payment Submitted!</b>\n"
            f"• <b>Order:</b> #{oid}\n"
            f"• <b>Buyer:</b> {user_mention} (<code>{uid}</code>)\n"
            f"• <b>Product:</b> {esc(o['product_name'])}\n"
            f"• <b>Amount:</b> {usd(o['price'])} USDT\n"
            f"• <b>Method:</b> {esc(o['payment_method'] or 'Manual')}\n"
            f"• <b>TxID:</b> <code>{esc(txid)}</code>\n\n"
            f"✅ Approve & deliver: /approve_{oid}\n"
            f"❌ Reject: /reject_{oid}"
        )


@dp.message(F.text.regexp(r"^/approve_(\d+)$"), is_admin)
@dp.message(Command("approve"), is_admin)
async def admin_approve_order(m: Message, command: CommandObject = None):
    oid = None
    if command and command.args:
        try:
            oid = int(command.args.strip())
        except ValueError:
            pass
    elif m.text and m.text.startswith("/approve_"):
        try:
            oid = int(m.text.split("_")[1])
        except ValueError:
            pass

    if not oid:
        return await m.answer("Usage: /approve <order_id> or /approve_<order_id>")

    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await m.answer(f"Order #{oid} not found.")
    if o["status"] == "completed" and o["payload"]:
        return await m.answer(f"Order #{oid} is already completed.")

    payload, err = await fulfill_order(m.bot, oid)
    if err:
        return await m.answer(f"⚠️ Failed to fulfill order #{oid}: {err}")

    try:
        buyer_uid = o["user_id"]
        if payload == "MANUAL_PENDING":
            await m.bot.send_message(
                buyer_uid,
                f"🎉 <b>Payment Confirmed for Order #{oid}!</b>\n\n"
                f"Item: <b>{esc(o['product_name'])}</b>\n\n"
                f"🟡 <i>This product is an assisted/manual setup service. Our support team has received your order and will deliver your credentials or setup link directly to you here shortly!</i>\n\n"
                f"If you have any questions, contact Support: {SUPPORT_URL}"
            )
            await m.answer(f"✅ Order #{oid} approved. Awaiting manual fulfillment with: <code>/deliver {oid} &lt;item&gt;</code>")
            return
        await m.bot.send_message(buyer_uid, t(buyer_uid, "purchased", oid=oid, payload=esc(payload)))
        await m.answer(f"✅ Order #{oid} approved and delivered to user {buyer_uid}!")
    except Exception as e:
        await m.answer(f"✅ Order #{oid} fulfilled, but could not message user: {e}\nPayload: <code>{esc(payload)}</code>")


@dp.message(F.text.regexp(r"^/reject_(\d+)$"), is_admin)
@dp.message(Command("reject"), is_admin)
async def admin_reject_order(m: Message, command: CommandObject = None):
    oid = None
    if command and command.args:
        try:
            oid = int(command.args.strip())
        except ValueError:
            pass
    elif m.text and m.text.startswith("/reject_"):
        try:
            oid = int(m.text.split("_")[1])
        except ValueError:
            pass

    if not oid:
        return await m.answer("Usage: /reject <order_id> or /reject_<order_id>")

    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await m.answer(f"Order #{oid} not found.")

    db.execute("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
    try:
        await m.bot.send_message(o["user_id"], f"❌ Order #{oid} payment verification could not be confirmed. If you believe this is an error, please contact support.")
    except Exception:
        pass
    await m.answer(f"Order #{oid} marked as rejected.")


@dp.callback_query(F.data.startswith("chk_cb:"))
async def check_cryptobot_payment(c: CallbackQuery):
    _, oid_str, inv_id_str = c.data.split(":")
    oid, inv_id = int(oid_str), int(inv_id_str)
    uid = c.from_user.id

    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await c.answer("Order not found.", show_alert=True)
    if o["status"] == "completed" and o["payload"]:
        await c.message.answer(t(uid, "purchased", oid=oid, payload=esc(o["payload"])))
        return await c.answer()

    paid = await check_cryptobot_invoice(inv_id)
    if paid:
        payload, err = await fulfill_order(c.bot, oid)
        if payload == "MANUAL_PENDING":
            await c.message.answer(
                f"🎉 <b>Payment Confirmed for Order #{oid}!</b>\n\n"
                f"Item: <b>{esc(o['product_name'])}</b>\n\n"
                f"🟡 <i>This product is an assisted/manual setup service. Our support team has received your order and will deliver your credentials or setup link directly to you here shortly!</i>\n\n"
                f"If you have any questions, contact Support: {SUPPORT_URL}"
            )
            await notify_admins(c.bot, f"🔔 <b>New Paid Manual Order #{oid}</b>\n• Item: <b>{esc(o['product_name'])}</b>\n• Buyer: <code>{uid}</code>\n• Paid: {usd(o['price'])}\n👉 Use <code>/deliver {oid} &lt;credentials/link&gt;</code> to complete.")
            await c.answer()
        elif payload:
            await c.message.answer(t(uid, "purchased", oid=oid, payload=esc(payload)))
            await notify_admins(c.bot, f"✅ Order #{oid} paid via @CryptoBot & delivered: {esc(o['product_name'])} ({usd(o['price'])}) to user {uid}")
            await c.answer()
        else:
            await c.answer(t(uid, err or "supplier_error"), show_alert=True)
            await notify_admins(c.bot, f"⚠️ Order #{oid} paid via @CryptoBot by user {uid} but delivery failed: {err}")
    else:
        await c.answer(t(uid, "pay_pending"), show_alert=True)


@dp.callback_query(F.data.startswith("manual_pay:"))
async def manual_pay_info(c: CallbackQuery):
    oid = int(c.data.split(":")[1])
    uid = c.from_user.id
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await c.answer("Order not found.", show_alert=True)
    details = manual_crypto_details()
    text = t(uid, "manual_crypto_info", oid=oid, name=esc(o["product_name"]), amount=usd(o["price"]), details=details)
    await edit(c, text, kb([[btn(t(uid, "btn_support"), url=SUPPORT_URL, style="primary")],
                            [btn(t(uid, "btn_back"), f"p:{o['product_id']}", style="danger")] ]))


@dp.callback_query(F.data == "orders")
@dp.message(Command("order"))
@dp.message(Command("orders"))
@dp.message(Command("purchases"))
async def recent_purchases(event: Message | CallbackQuery):
    """View recent purchases for the current user."""
    uid = event.from_user.id
    rows = db.execute(
        "SELECT * FROM orders WHERE user_id=? AND payload IS NOT NULL AND status='completed' ORDER BY id DESC LIMIT 10",
        (uid,)
    ).fetchall()

    if not rows:
        text = t(uid, "no_orders")
    else:
        text = t(uid, "orders_title") + "\n\n" + "\n\n".join(
            f"📦 <b>Order #{o['id']}</b> · {esc(o['product_name'])} · {usd(o['price'])}\n<code>{esc(o['payload'])}</code>"
            for o in rows
        )

    markup = kb([[btn(t(uid, "btn_back"), "home", style="danger")]])
    if isinstance(event, CallbackQuery):
        await edit(event, text, markup)
    else:
        await event.answer(text, reply_markup=markup)


@dp.message(Command("support"))
async def cmd_support(m: Message):
    uid = m.from_user.id
    get_user(uid, m.from_user.language_code)
    support_emoji_id = get_custom_emoji_id("support")
    markup = kb([
        [btn(t(uid, "btn_support"), url=SUPPORT_URL, style="primary", emoji_id=support_emoji_id)],
        [btn(t(uid, "btn_back"), "home", style="danger")]
    ])
    await m.answer(
        f"💬 <b>Support</b>\n\nNeed assistance? Contact our support team directly:\n{SUPPORT_URL}",
        reply_markup=markup
    )


@dp.callback_query(F.data == "lang")
async def lang_menu(c: CallbackQuery):
    uid = c.from_user.id
    rows = [[btn(name, f"setlang:{code}", style="primary")] for code, name in LANG_NAMES.items()]
    rows.append([btn(t(uid, "btn_back"), "home", style="danger")])
    await edit(c, t(uid, "lang_choose"), kb(rows))


@dp.callback_query(F.data.startswith("setlang:"))
async def set_lang(c: CallbackQuery):
    code = c.data.split(":")[1]
    if code in TR:
        get_user(c.from_user.id)
        db.execute("UPDATE users SET lang=? WHERE id=?", (code, c.from_user.id))
    uid = c.from_user.id
    await edit(c, t(uid, "welcome"), home_kb(uid))


# --------------------------------------------------------------------------
# Admin commands
# --------------------------------------------------------------------------
@dp.message(Command("admin"), is_admin)
async def admin_help(m: Message):
    await m.answer(
        "<b>Admin Commands</b>\n\n"
        "<b>Catalog & Products</b>\n"
        "/products - List all products with margins\n"
        "/addproduct &lt;price_usd&gt; &lt;name&gt; [| description]\n"
        "/setprice &lt;product_id&gt; &lt;price_usd&gt;\n"
        "/setmargin &lt;product_id&gt; &lt;margin_%&gt; (e.g. /setmargin 1 60%)\n"
        "/toggle &lt;product_id&gt;  (activate/deactivate)\n"
        "/addstock &lt;product_id&gt; &lt;item&gt;  (manual items, 1 per line)\n"
        "/refund &lt;order_id&gt;\n"
        "/stats - View sales stats\n"
        "/broadcast &lt;text&gt; - Broadcast message\n\n"
        "🎨 <b>Brand Custom Emojis</b>\n"
        "/setemoji &lt;key&gt; [emoji] - Set official brand custom emoji ID\n"
        "/emojis - View configured brand custom emojis\n"
        "/delemoji &lt;key&gt; - Remove custom emoji\n\n"
        "🌐 <b>Supplier Catalog (Automated)</b>\n"
        "/supplier - Supplier balance & account info\n"
        "/s_products - List available supplier products\n"
        "/s_sync [markup_%] - Auto-sync catalog with profit margin\n"
        "/s_link &lt;local_id&gt; &lt;supplier_id&gt; - Link item to supplier\n"
        "/s_unlink &lt;local_id&gt; - Revert to manual stock\n"
        "/s_orders - Recent supplier orders"
    )


@dp.message(Command("setemoji"), is_admin)
async def admin_setemoji(m: Message, command: CommandObject):
    """
    Sets custom emoji ID for a brand, category, or UI button.
    Usage: /setemoji <key> [custom_emoji_or_id]
    Examples:
      /setemoji chatgpt  (with custom emoji sent in the message)
      /setemoji chatgpt 5368324170671202288
      /setemoji gemini
    """
    if not command.args:
        avail_keys = ", ".join(k for k, _, _ in CATEGORIES)
        return await m.answer(
            f"<b>Usage:</b> <code>/setemoji &lt;key&gt; [emoji]</code>\n\n"
            f"Send with a custom emoji or paste its numeric ID.\n\n"
            f"<b>Available keys:</b>\n<code>{avail_keys}, shop, orders, lang, support, news, search, all_items, back</code>"
        )

    parts = command.args.split(None, 1)
    key = parts[0].strip().lower()

    # Look for custom_emoji in message entities
    emoji_id = None
    if m.entities:
        for ent in m.entities:
            if ent.type == "custom_emoji" and ent.custom_emoji_id:
                emoji_id = str(ent.custom_emoji_id)
                break

    # If not found in entities, check if numeric string passed in text
    if not emoji_id and len(parts) > 1:
        arg_val = "".join(c for c in parts[1].strip() if c.isdigit())
        if arg_val:
            emoji_id = arg_val

    if not emoji_id:
        return await m.answer(
            f"⚠️ Could not find custom emoji ID for <b>{esc(key)}</b>.\n"
            f"Please insert a custom emoji from your Telegram pack in the command or provide its numeric ID:\n"
            f"<code>/setemoji {esc(key)} &lt;custom_emoji_or_id&gt;</code>"
        )

    db.execute(
        "INSERT INTO custom_emojis(key, emoji_id) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET emoji_id=excluded.emoji_id",
        (key, emoji_id)
    )
    await m.answer(
        f"✅ Brand emoji for <b>{esc(key)}</b> updated!\n"
        f"• Custom Emoji ID: <code>{esc(emoji_id)}</code>"
    )


@dp.message(Command("emojis"), is_admin)
async def admin_list_emojis(m: Message):
    """List all configured brand custom emojis."""
    rows = db.execute("SELECT key, emoji_id FROM custom_emojis ORDER BY key").fetchall()
    if not rows:
        return await m.answer(
            "No custom emojis configured yet.\n"
            "Use <code>/setemoji &lt;key&gt; &lt;emoji&gt;</code> to set official brand custom emojis from your Telegram pack."
        )

    lines = ["<b>Configured Brand Custom Emojis:</b>\n"]
    for r in rows:
        lines.append(f"• <b>{esc(r['key'])}</b>: <code>{esc(r['emoji_id'])}</code>")
    await m.answer("\n".join(lines))


@dp.message(Command("delemoji"), is_admin)
async def admin_del_emoji(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("Usage: /delemoji <key>")
    key = command.args.strip().lower()
    db.execute("DELETE FROM custom_emojis WHERE key=?", (key,))
    await m.answer(f"Custom emoji for <b>{esc(key)}</b> removed.")


@dp.message(Command("products"), is_admin)
async def admin_products(m: Message):
    rows = db.execute("SELECT * FROM products ORDER BY id").fetchall()
    if not rows:
        return await m.answer("No products.")
    lines = ["📦 <b>Store Products (Color-Coded Status):</b>\n"]
    for p in rows:
        is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
        sup_stock = p["supplier_stock"] if "supplier_stock" in p.keys() else None
        deliv = p["delivery_type"] if ("delivery_type" in p.keys() and p["delivery_type"]) else ("auto" if is_auto else "manual")

        if deliv == "manual":
            status_badge = "🟡 [MANUAL]"
        elif is_auto and sup_stock is not None and sup_stock == 0:
            status_badge = "🔴 [OUT OF STOCK]"
        elif is_auto and not p["active"]:
            status_badge = "🔴 [OUT OF STOCK]"
        elif is_auto:
            stock_str = f" ({sup_stock} pcs)" if sup_stock is not None else ""
            status_badge = f"🟢 [AUTO{stock_str}]"
        else:
            local_cnt = stock_count(p["id"])
            if local_cnt > 0:
                status_badge = f"🟢 [LOCAL ({local_cnt} pcs)]"
            else:
                status_badge = "🔴 [OUT OF STOCK]"

        margin_info = ""
        if p["margin"] is not None:
            margin_info = f" · margin +{p['margin']}%"
        elif p["supplier_price"]:
            cost = p["supplier_price"]
            calc_m = round(((p["price"] - cost) / cost) * 100)
            margin_info = f" · margin ~{calc_m}%"

        cost_disp = f" (cost {usd(p['supplier_price'])})" if p["supplier_price"] else ""
        cat_disp = f" [{p['category']}]" if ("category" in p.keys() and p["category"]) else ""
        lines.append(
            f"{status_badge} #{p['id']}. <b>{esc(p['name'])}</b>{cat_disp} · {usd(p['price'])}{cost_disp}{margin_info}"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        for i in range(0, len(text), 3800):
            await m.answer(text[i:i+3800])
    else:
        await m.answer(text)


@dp.message(Command("addproduct"), is_admin)
async def admin_addproduct(m: Message, command: CommandObject):
    try:
        price, rest = command.args.split(None, 1)
        name, _, desc = rest.partition("|")
        cat = detect_category(name.strip())
        pid = db.execute("INSERT INTO products(name, description, price, category) VALUES(?,?,?,?)",
                         (name.strip(), desc.strip(), round(float(price) * 100), cat)).lastrowid
        await m.answer(f"Product #{pid} added ({cat}).")
    except Exception:
        await m.answer("Usage: /addproduct <price_usd> <name> [| description]")


@dp.message(Command("setprice"), is_admin)
async def admin_setprice(m: Message, command: CommandObject):
    try:
        pid, price = command.args.split()
        db.execute("UPDATE products SET price=? WHERE id=?", (round(float(price) * 100), int(pid)))
        await m.answer("Price updated.")
    except Exception:
        await m.answer("Usage: /setprice <product_id> <price_usd>")


@dp.message(Command("setmargin"), is_admin)
async def admin_setmargin(m: Message, command: CommandObject):
    try:
        pid, raw_margin = command.args.split()
        pid = int(pid)
        margin = parse_profit_margin(raw_margin)
        p = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            return await m.answer(f"Product #{pid} not found.")

        cost = p["supplier_price"] or p["price"]
        new_price = max(100, round(cost * (1 + margin / 100)))
        db.execute("UPDATE products SET margin=?, price=? WHERE id=?", (margin, new_price, pid))

        cost_info = f"• Wholesale Cost: <b>{usd(cost)}</b>\n" if p["supplier_price"] else ""
        await m.answer(
            f"✅ Profit margin for #{pid} (<b>{esc(p['name'])}</b>) set to <b>{margin}%</b>.\n"
            f"{cost_info}"
            f"• New Selling Price: <b>{usd(new_price)}</b>"
        )
    except Exception:
        await m.answer("Usage: /setmargin <product_id> <margin_percent>\nExamples: /setmargin 1 50% or /setmargin 1 35")


@dp.message(Command("toggle"), is_admin)
async def admin_toggle(m: Message, command: CommandObject):
    try:
        db.execute("UPDATE products SET active=1-active WHERE id=?", (int(command.args),))
        await m.answer("Toggled.")
    except Exception:
        await m.answer("Usage: /toggle <product_id>")


@dp.message(Command("addstock"), is_admin)
async def admin_addstock(m: Message, command: CommandObject):
    try:
        pid, items = command.args.split(None, 1)
        lines = [x.strip() for x in items.splitlines() if x.strip()]
        db.executemany("INSERT INTO stock(product_id, payload) VALUES(?,?)", [(int(pid), x) for x in lines])
        await m.answer(f"Added {len(lines)} item(s). Stock now {stock_count(int(pid))}.")
    except Exception:
        await m.answer("Usage: /addstock <product_id> <item>  (one item per line)")


@dp.message(Command("refund"), is_admin)
async def admin_refund(m: Message, command: CommandObject):
    try:
        oid = int(command.args)
        db.execute("BEGIN IMMEDIATE")
        o = db.execute("SELECT * FROM orders WHERE id=? AND refunded=0", (oid,)).fetchone()
        if not o:
            db.execute("ROLLBACK")
            return await m.answer("Order not found or already refunded.")
        db.execute("UPDATE orders SET refunded=1 WHERE id=?", (oid,))
        db.execute("COMMIT")
        await m.answer(f"Order #{oid} marked as refunded.")
    except ValueError:
        await m.answer("Usage: /refund <order_id>")


@dp.message(Command("deliver"), is_admin)
async def admin_deliver(m: Message, command: CommandObject):
    try:
        oid_str, payload = command.args.split(None, 1)
        oid = int(oid_str)
        o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return await m.answer(f"Order #{oid} not found.")

        db.execute("UPDATE orders SET payload=?, status='completed' WHERE id=?", (payload.strip(), oid))

        try:
            await m.bot.send_message(
                o["user_id"],
                f"🎉 <b>Your Order #{oid} is Ready!</b>\n\n"
                f"Item: <b>{esc(o['product_name'])}</b>\n\n"
                f"📦 <b>Delivered Item / Credentials:</b>\n"
                f"<code>{esc(payload.strip())}</code>\n\n"
                f"Thank you for shopping with us!"
            )
            await m.answer(f"✅ Order #{oid} delivered to user {o['user_id']}!")
        except Exception as e:
            await m.answer(f"✅ Order #{oid} marked completed, but could not message user: {e}\nPayload: <code>{esc(payload)}</code>")
    except Exception:
        await m.answer("Usage: /deliver <order_id> <delivered_content_or_link>")


@dp.message(Command("stats"), is_admin)
async def admin_stats(m: Message):
    def q(sql):
        return db.execute(sql).fetchone()[0] or 0

    users = q("SELECT COUNT(*) FROM users")
    orders_n = q("SELECT COUNT(*) FROM orders WHERE status='completed' AND refunded=0")
    revenue = q("SELECT SUM(price) FROM orders WHERE status='completed' AND refunded=0")
    pending = q("SELECT COUNT(*) FROM orders WHERE status='pending'")
    await m.answer(
        f"👥 Total buyers: {users}\n"
        f"📦 Completed purchases: {orders_n}\n"
        f"⏳ Pending checkouts: {pending}\n"
        f"💰 Total revenue: {usd(revenue)}"
    )


@dp.message(Command("broadcast"), is_admin)
async def admin_broadcast(m: Message, command: CommandObject):
    if not command.args:
        return await m.answer("Usage: /broadcast <text>")
    sent = 0
    for u in db.execute("SELECT id FROM users").fetchall():
        try:
            await m.bot.send_message(u["id"], command.args)
            sent += 1
        except Exception:
            pass
        await asyncio.sleep(0.05)
    await m.answer(f"Sent to {sent} users.")


# --------------------------------------------------------------------------
# Supplier Admin Commands (Multi-Supplier Registry)
# --------------------------------------------------------------------------
@dp.message(Command("supplier", "suppliers"), is_admin)
async def admin_supplier_info(m: Message, command: CommandObject):
    registered = supplier_registry.list_suppliers()
    if not registered:
        return await m.answer("⚠️ No suppliers registered. Configure <code>RESELLER_API_KEY</code> or <code>SUPPLIER_&lt;NAME&gt;_KEY</code> in .env.")

    arg = (command.args or "").strip().lower()
    if arg and arg in registered:
        sup_client = registered[arg]
        try:
            bal_res = await sup_client.get_balance()
            bal_str = f"${bal_res.get('balance', 0):.2f} {bal_res.get('currency', 'USD')}"
        except Exception as e:
            bal_str = f"Error: {e}"

        try:
            info_res = await sup_client.get_info()
            info_text = json.dumps(info_res, indent=2, ensure_ascii=False)
            if len(info_text) > 400:
                info_text = info_text[:400] + "..."
        except Exception as e:
            info_text = f"Error: {e}"

        p_count = db.execute("SELECT COUNT(*) FROM products WHERE supplier=? AND active=1", (arg,)).fetchone()[0]

        return await m.answer(
            f"🌐 <b>Supplier: <code>{esc(arg)}</code></b>\n"
            f"<b>Base URL:</b> <code>{esc(sup_client.base_url)}</code>\n"
            f"<b>Balance:</b> <b>{esc(bal_str)}</b>\n"
            f"<b>Active Linked Products:</b> {p_count}\n\n"
            f"<b>Account Info:</b>\n<pre>{esc(info_text)}</pre>"
        )

    # All suppliers overview
    lines = ["🌐 <b>Configured Suppliers / Resellers:</b>\n"]
    for name, client in registered.items():
        try:
            bal_res = await client.get_balance()
            bal_disp = f"${bal_res.get('balance', 0):.2f} {bal_res.get('currency', 'USD')}"
        except Exception as e:
            bal_disp = f"Error ({e})"

        p_count = db.execute("SELECT COUNT(*) FROM products WHERE supplier=? AND active=1", (name,)).fetchone()[0]
        is_def = " (default)" if name == supplier_registry.default_name() else ""
        lines.append(
            f"• <b>{esc(name.upper())}</b>{is_def}\n"
            f"  URL: <code>{esc(client.base_url)}</code>\n"
            f"  Balance: <b>{esc(bal_disp)}</b> · Products: {p_count}\n"
        )

    lines.append("<i>Tip: Type <code>/supplier &lt;name&gt;</code> for details or <code>/s_sync &lt;name&gt; [margin]</code> to sync products.</i>")
    await m.answer("\n".join(lines))


@dp.message(Command("s_products"), is_admin)
async def admin_supplier_products(m: Message, command: CommandObject):
    sup_name = (command.args or "").strip().lower() or supplier_registry.default_name()
    sup_client = supplier_registry.get(sup_name)
    if not sup_client or not sup_client.api_key:
        return await m.answer(f"⚠️ Supplier <code>{esc(sup_name)}</code> not found or has no API key.")

    try:
        products = await sup_client.get_products()
    except Exception as e:
        return await m.answer(f"Failed to fetch supplier catalog from '{sup_name}': {esc(str(e))}")

    if not products:
        return await m.answer(f"No products found in catalog for '{sup_name}'.")

    local_map = {row["supplier_product_id"]: row for row in db.execute(
        "SELECT id, name, price, supplier_product_id FROM products WHERE supplier=? AND supplier_product_id IS NOT NULL", (sup_name,)).fetchall()}

    lines = [f"<b>Supplier '{esc(sup_name)}' Catalog Products:</b>\n"]
    for p in products:
        pid = p["id"]
        linked = local_map.get(pid)
        status = f"✅ In shop (Local #{linked['id']}, {usd(linked['price'])})" if linked else "⚪ Not linked"
        stock_disp = f", stock: {p['stock']}" if p.get("stock") is not None else ""
        lines.append(
            f"• <code>{esc(pid)}</code>: <b>{esc(p['name'])}</b>\n"
            f"  Cost: {usd(p['price_cents'])}{stock_disp} · {status}"
        )

    text = "\n".join(lines)
    if len(text) > 4000:
        for i in range(0, len(text), 3800):
            await m.answer(text[i:i+3800])
    else:
        await m.answer(text)


async def perform_supplier_sync(
    target_suppliers: Optional[List[str]] = None,
    markup: int = PROFIT_MARGIN,
) -> Tuple[int, int, int, List[str]]:
    """
    Synchronizes catalog and live inventory from reseller API.
    Updates retail prices with markup, updates supplier_stock, sets auto delivery,
    and reconciles unlisted or depleted items as out of stock.
    Returns: (total_added, total_updated, total_items, errors)
    """
    registered = supplier_registry.list_suppliers()
    if not registered:
        return 0, 0, 0, ["No suppliers registered"]

    if not target_suppliers:
        target_suppliers = list(registered.keys())

    total_added = 0
    total_updated = 0
    total_items = 0
    errors = []

    for sup_name in target_suppliers:
        client = registered.get(sup_name)
        if not client or not client.api_key:
            errors.append(f"'{sup_name}': API key not configured")
            continue

        try:
            sup_products = await client.get_products()
        except Exception as e:
            errors.append(f"'{sup_name}': {e}")
            continue

        seen_supplier_ids = set()
        for sp in sup_products:
            sp_id = str(sp["id"])
            seen_supplier_ids.add(sp_id)
            cost_cents = sp["price_cents"]
            stock = sp.get("stock")
            cat = detect_category(sp["name"])

            existing = db.execute(
                "SELECT id, margin FROM products WHERE supplier_product_id=? AND supplier=?",
                (sp_id, sup_name)
            ).fetchone()

            # If not matched by supplier_product_id, check if unlinked product exists with identical or similar name
            if not existing:
                existing = db.execute(
                    "SELECT id, margin FROM products WHERE supplier_product_id IS NULL AND LOWER(name)=LOWER(?)",
                    (sp["name"],)
                ).fetchone()

            if not existing:
                clean_sp_name = sp["name"].lower()
                if "gemini" in clean_sp_name and "18" in clean_sp_name:
                    existing = db.execute("SELECT id, margin FROM products WHERE supplier_product_id IS NULL AND name LIKE '%Gemini%18%'").fetchone()
                elif "duolingo" in clean_sp_name and ("12" in clean_sp_name or "super" in clean_sp_name):
                    existing = db.execute("SELECT id, margin FROM products WHERE supplier_product_id IS NULL AND name LIKE '%Duolingo%'").fetchone()

            item_margin = existing["margin"] if (existing and existing["margin"] is not None) else markup
            retail_cents = max(100, round(cost_cents * (1 + item_margin / 100)))
            is_active = 0 if (stock is not None and stock == 0) else 1

            if existing:
                db.execute("""
                    UPDATE products
                    SET name=?, description=?, price=?, supplier_price=?, auto_fulfill=1, active=?, category=?, supplier=?,
                        supplier_stock=?, delivery_type='auto', supplier_product_id=?
                    WHERE id=?
                """, (sp["name"], sp["description"], retail_cents, cost_cents, is_active, cat, sup_name, stock, sp_id, existing["id"]))
                total_updated += 1
            else:
                db.execute("""
                    INSERT INTO products(name, description, price, supplier_product_id, supplier_price, auto_fulfill, active, category, supplier, supplier_stock, delivery_type)
                    VALUES(?,?,?,?,?,1,?,?,?,?, 'auto')
                """, (sp["name"], sp["description"], retail_cents, sp_id, cost_cents, is_active, cat, sup_name, stock))
                total_added += 1

        # Mark products linked to this supplier that are no longer in feed as out of stock / inactive
        if seen_supplier_ids:
            placeholders = ",".join(["?"] * len(seen_supplier_ids))
            db.execute(
                f"UPDATE products SET supplier_stock=0, active=0 WHERE supplier=? AND delivery_type='auto' AND supplier_product_id NOT IN ({placeholders})",
                [sup_name] + list(seen_supplier_ids)
            )

        total_items += len(sup_products)

    return total_added, total_updated, total_items, errors


@dp.message(Command("s_sync"), is_admin)
async def admin_supplier_sync(m: Message, command: CommandObject):
    registered = supplier_registry.list_suppliers()
    if not registered:
        return await m.answer("⚠️ No suppliers configured in .env.")

    args = (command.args or "").strip().split()
    target_suppliers = []
    markup = PROFIT_MARGIN

    if not args:
        target_suppliers = [supplier_registry.default_name()]
    elif args[0].lower() == "all":
        target_suppliers = list(registered.keys())
        if len(args) > 1:
            markup = parse_profit_margin(args[1], default=markup)
    elif args[0].lower() in registered:
        target_suppliers = [args[0].lower()]
        if len(args) > 1:
            markup = parse_profit_margin(args[1], default=markup)
    else:
        markup = parse_profit_margin(args[0], default=markup)
        target_suppliers = [supplier_registry.default_name()]

    total_added, total_updated, total_items, errors = await perform_supplier_sync(target_suppliers, markup)

    # Gather color-coded status of all products in DB
    all_prods = db.execute("SELECT * FROM products ORDER BY id").fetchall()
    in_stock_auto = []
    manual_items = []
    out_of_stock_items = []

    for p in all_prods:
        is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])

        if not is_product_available(p):
            out_of_stock_items.append(p)
        elif is_auto:
            in_stock_auto.append(p)
        else:
            manual_items.append(p)

    lines = [
        "🔄 <b>Catalog Sync & Health Report</b>\n",
        f"• Suppliers Synced: <b>{', '.join(target_suppliers)}</b>",
        f"• Default Margin: <b>{markup}%</b>",
        f"• Total Processed from Feed: <b>{total_items}</b>\n",
        "📊 <b>Status Breakdown:</b>",
        f"🟢 <b>In Stock (Auto-Fulfill):</b> <b>{len(in_stock_auto)}</b> items",
        f"🟡 <b>Manual / Assisted Services:</b> <b>{len(manual_items)}</b> items",
        f"🔴 <b>Out of Stock / Unavailable:</b> <b>{len(out_of_stock_items)}</b> items\n",
    ]

    if in_stock_auto:
        lines.append("🟢 <b>Available Auto-Delivery (Samples):</b>")
        for p in in_stock_auto[:8]:
            s_disp = f" (Stock: {p['supplier_stock']})" if ("supplier_stock" in p.keys() and p['supplier_stock'] is not None) else ""
            lines.append(f" • <code>#{p['id']}</code> {esc(p['name'])} · {usd(p['price'])}{s_disp}")
        if len(in_stock_auto) > 8:
            lines.append(f" <i>...and {len(in_stock_auto) - 8} more in-stock items</i>\n")
        else:
            lines.append("")

    if manual_items:
        lines.append("🟡 <b>Manual / Assisted Services:</b>")
        for p in manual_items[:8]:
            lines.append(f" • <code>#{p['id']}</code> {esc(p['name'])} · {usd(p['price'])}")
        if len(manual_items) > 8:
            lines.append(f" <i>...and {len(manual_items) - 8} more manual services</i>\n")
        else:
            lines.append("")

    if out_of_stock_items:
        lines.append("🔴 <b>Out of Stock at Supplier:</b>")
        for p in out_of_stock_items[:8]:
            lines.append(f" • <code>#{p['id']}</code> {esc(p['name'])}")
        if len(out_of_stock_items) > 8:
            lines.append(f" <i>...and {len(out_of_stock_items) - 8} more out-of-stock items</i>\n")
        else:
            lines.append("")

    if errors:
        lines.append("⚠️ <b>Sync Warnings / Errors:</b>")
        for err in errors:
            lines.append(f" • {esc(err)}")

    res_msg = "\n".join(lines).strip()
    await m.answer(res_msg)


@dp.message(Command("s_link"), is_admin)
async def admin_supplier_link(m: Message, command: CommandObject):
    try:
        parts = command.args.split()
        local_id = int(parts[0])
        sup_id = parts[1].strip()
        sup_name = parts[2].strip().lower() if len(parts) > 2 else supplier_registry.default_name()

        p = db.execute("SELECT * FROM products WHERE id=?", (local_id,)).fetchone()
        if not p:
            return await m.answer(f"Product #{local_id} not found.")

        db.execute(
            "UPDATE products SET supplier_product_id=?, supplier=?, auto_fulfill=1 WHERE id=?",
            (sup_id, sup_name, local_id)
        )
        await m.answer(f"Product #{local_id} ({esc(p['name'])}) linked to supplier '<b>{esc(sup_name)}</b>' product <code>{esc(sup_id)}</code>.")
    except Exception:
        await m.answer("Usage: /s_link <local_product_id> <supplier_product_id> [supplier_name]")


@dp.message(Command("s_unlink"), is_admin)
async def admin_supplier_unlink(m: Message, command: CommandObject):
    try:
        local_id = int(command.args)
        p = db.execute("SELECT * FROM products WHERE id=?", (local_id,)).fetchone()
        if not p:
            return await m.answer(f"Product #{local_id} not found.")
        db.execute("UPDATE products SET supplier_product_id=NULL, auto_fulfill=0 WHERE id=?", (local_id,))
        await m.answer(f"Product #{local_id} ({esc(p['name'])}) unlinked from supplier. Now uses local stock.")
    except Exception:
        await m.answer("Usage: /s_unlink <local_product_id>")


@dp.message(Command("s_orders"), is_admin)
async def admin_supplier_orders(m: Message):
    if not RESELLER_API_KEY:
        return await m.answer("⚠️ <code>RESELLER_API_KEY</code> is not configured in .env.")
    try:
        orders = await reseller.get_orders()
        if not orders:
            return await m.answer("No orders found at supplier.")
        lines = ["<b>Recent Supplier Orders:</b>\n"]
        for o in orders[:10]:
            code = o.get("orderCode") or o.get("id") or "N/A"
            item = o.get("productId") or o.get("product_name") or "Item"
            cost = o.get("price") or o.get("cost") or ""
            lines.append(f"• <code>{esc(str(code))}</code> - {esc(str(item))} {esc(str(cost))}")
        await m.answer("\n".join(lines))
    except Exception as e:
        await m.answer(f"Failed to fetch supplier orders: {esc(str(e))}")


async def periodic_supplier_sync(interval_sec: int = 600):
    """Background task to periodically synchronize supplier products and inventory."""
    while True:
        try:
            await asyncio.sleep(interval_sec)
            added, updated, total, errors = await perform_supplier_sync()
            log.info("Periodic supplier sync completed: %d updated, %d added of %d total (errors: %s)", updated, added, total, len(errors))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("Periodic supplier sync encountered error: %s", e)


# --------------------------------------------------------------------------
# Entrypoint
# --------------------------------------------------------------------------
async def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    app = web.Application()
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Health check server listening on :%s", PORT)

    # Initial sync with reseller API on startup
    try:
        init_added, init_updated, init_total, init_errs = await perform_supplier_sync()
        log.info("Initial supplier sync: %d updated, %d added of %d total (errors: %d)", init_updated, init_added, init_total, len(init_errs))
    except Exception as e:
        log.warning("Initial supplier sync on startup failed: %s", e)

    sync_task = asyncio.create_task(periodic_supplier_sync(600))

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_my_commands([
            BotCommand(command="start", description="Start bot"),
            BotCommand(command="menu", description="Open main menu"),
            BotCommand(command="shop", description="Open shop"),
            BotCommand(command="order", description="Order history"),
            BotCommand(command="support", description="Contacts support"),
        ])
        await dp.start_polling(bot)
    finally:
        sync_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass
        await supplier_registry.close_all()
        await reseller.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
