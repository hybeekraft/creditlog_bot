"""
Telegram store bot for digital products.
Supports guest checkout, automated delivery via supplier APIs or local stock,
crypto, card, and bank transfers, with multi-language support.
"""
import asyncio
import html
import json
import logging
import os
import sqlite3
import sys
import time
from typing import List, Optional, Tuple

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (BotCommand, BotCommandScopeChat, CallbackQuery, FSInputFile, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from dotenv import load_dotenv

from reseller import ResellerClient, ResellerAPIError, SupplierRegistry
import onchain

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("store")

# Bot settings and environment
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = set()
for raw_id in os.environ.get("ADMIN_IDS", "").split(","):
    clean_id = raw_id.strip()
    if clean_id.isdigit():
        ADMIN_IDS.add(int(clean_id))

def safe_int_env(key: str, default: int) -> int:
    val = (os.environ.get(key, "") or "").strip()
    try:
        return int(val) if val else default
    except (ValueError, TypeError):
        return default

def is_admin(m: Message) -> bool:
    return bool(m.from_user and m.from_user.id in ADMIN_IDS)

def usd(c) -> str:
    try:
        val = float(c) if c is not None else 0.0
        return f"${val / 100.0:.2f}"
    except (ValueError, TypeError):
        return "$0.00"

esc = html.escape
SUPPORT_URL = os.environ.get("SUPPORT_URL", "https://t.me/your_support")
CHANNEL_URL = os.environ.get("CHANNEL_URL", "https://t.me/your_channel")
REQUIRED_CHANNELS_RAW = os.environ.get("REQUIRED_CHANNELS", "").strip()
LOW_STOCK = safe_int_env("LOW_STOCK", 2)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
db_env = os.environ.get("DB_PATH", "store.db")
if db_env == ":memory:":
    DB_PATH = ":memory:"
elif db_env == "/data/store.db" and sys.platform == "win32" and not os.path.exists("/.dockerenv"):
    DB_PATH = os.path.join(BASE_DIR, "store.db")
elif os.path.isabs(db_env):
    DB_PATH = db_env
else:
    DB_PATH = os.path.join(BASE_DIR, db_env)
PORT = safe_int_env("PORT", 8080)

def parse_profit_margin(val: Optional[str], default: int = 45) -> int:
    if not val:
        return default
    cleaned = "".join(c for c in str(val) if c.isdigit())
    return int(cleaned) if cleaned else default

# Profit margin
raw_margin = os.environ.get("PROFIT_MARGIN") or os.environ.get("RESELLER_MARKUP_PERCENT") or "45%"
PROFIT_MARGIN = parse_profit_margin(raw_margin, default=45)
RESELLER_MARKUP_PERCENT = PROFIT_MARGIN

# Supplier API setup
RESELLER_BASE_URL = (os.environ.get("RESELLER_BASE_URL") or "https://api-geminipro.ignorelist.com/api/reseller/v1").rstrip("/")
RESELLER_API_KEY = os.environ.get("RESELLER_API_KEY", "")
supplier_registry = SupplierRegistry.from_env()
reseller = supplier_registry.get("default") or ResellerClient(base_url=RESELLER_BASE_URL, api_key=RESELLER_API_KEY)
if "default" not in supplier_registry.list_suppliers():
    supplier_registry.register("default", reseller, is_default=True)

# Crypto payment settings
CRYPTOBOT_TOKEN = os.environ.get("CRYPTOBOT_TOKEN", "")
BINANCE_PAY_ID = (os.environ.get("BINANCE_PAY_ID", "") or "").strip()
if not BINANCE_PAY_ID or BINANCE_PAY_ID.startswith("your_"):
    BINANCE_PAY_ID = "76177351"  # Default backup ID


# Default USDT wallet addresses
CRYPTO_USDT_BEP20 = (os.environ.get("CRYPTO_USDT_BEP20", "") or "").strip()
if not CRYPTO_USDT_BEP20 or CRYPTO_USDT_BEP20.startswith("0xYour") or CRYPTO_USDT_BEP20.startswith("0x4B20"):
    CRYPTO_USDT_BEP20 = "0xe37301d1cb7E1d479a1F1A78d6A2EAed317412Db"

CRYPTO_USDT_TRC20 = (os.environ.get("CRYPTO_USDT_TRC20", "") or "").strip()
if not CRYPTO_USDT_TRC20 or CRYPTO_USDT_TRC20.startswith("TYour") or CRYPTO_USDT_TRC20.startswith("TR7NH"):
    CRYPTO_USDT_TRC20 = "TUaDYfZ2fBRXSRDm9d6kN9j2J7bi7ZsBLW"

# Default exchange rate
DEFAULT_USD_NGN_RATE = safe_int_env("USD_NGN_RATE", 1450)

# Bachs payment gateway settings
BACHS_API_KEY = os.environ.get("BACHS_API_KEY", "").strip()
BACHS_CURRENCY = (os.environ.get("BACHS_CURRENCY", "USD") or "USD").strip().upper()
BACHS_CALLBACK_URL = os.environ.get("BACHS_CALLBACK_URL", "").strip()

# Database connection and schema
db = sqlite3.connect(DB_PATH, isolation_level=None, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY,
  lang TEXT DEFAULT 'en',
  currency TEXT DEFAULT 'USD',
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
  fixed_price INTEGER DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT);
CREATE TABLE IF NOT EXISTS custom_emojis(
  key TEXT PRIMARY KEY,
  emoji_id TEXT NOT NULL,
  created TEXT DEFAULT CURRENT_TIMESTAMP);
""")

try:
    db.execute("ALTER TABLE products ADD COLUMN fixed_price INTEGER DEFAULT 0")
except sqlite3.OperationalError:
    pass


def get_setting(key: str, default: str = "") -> str:
    try:
        row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default
    except sqlite3.OperationalError:
        db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        return default


def set_setting(key: str, value: str):
    try:
        db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )
    except sqlite3.OperationalError:
        db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        db.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value)
        )


def get_default_margin() -> int:
    """Default profit margin percentage."""
    return parse_profit_margin(get_setting("profit_margin", str(PROFIT_MARGIN)), default=45)


def get_usd_ngn_rate() -> int:
    """Current USD to NGN exchange rate."""
    raw = get_setting("usd_ngn_rate", str(DEFAULT_USD_NGN_RATE))
    try:
        val = int(raw)
        return val if val > 0 else DEFAULT_USD_NGN_RATE
    except ValueError:
        return DEFAULT_USD_NGN_RATE


def set_usd_ngn_rate(rate: int):
    set_setting("usd_ngn_rate", str(max(1, rate)))


def get_bank_accounts() -> list[dict[str, str]]:
    """Configured bank accounts for transfer."""
    raw = get_setting("bank_accounts", "")
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                res = []
                for item in parsed:
                    if isinstance(item, dict):
                        b_name = str(item.get("bank_name", "")).strip()
                        acc_num = "".join(c for c in str(item.get("account_number", "")) if c.isdigit())
                        acc_name = str(item.get("account_name", "")).strip()
                        if b_name and len(acc_num) >= 8:
                            res.append({
                                "bank_name": b_name,
                                "account_number": acc_num,
                                "account_name": acc_name or "CreditLog Store"
                            })
                return res
        except Exception:
            pass

    # Fallback to single account if list is empty
    bank_name = (get_setting("bank_name", "") or os.environ.get("BANK_NAME", "")).strip()
    account_number = "".join(c for c in (get_setting("bank_account_number", "") or os.environ.get("BANK_ACCOUNT_NUMBER", "")) if c.isdigit())
    account_name = (get_setting("bank_account_name", "") or os.environ.get("BANK_ACCOUNT_NAME", "")).strip()
    if bank_name and len(account_number) >= 8:
        return [{
            "bank_name": bank_name,
            "account_number": account_number,
            "account_name": account_name or "CreditLog Store"
        }]
    return []


def save_bank_accounts(accounts: list[dict[str, str]]):
    """Save bank accounts to settings."""
    set_setting("bank_accounts", json.dumps(accounts))
    if accounts:
        set_setting("bank_name", accounts[0]["bank_name"])
        set_setting("bank_account_number", accounts[0]["account_number"])
        set_setting("bank_account_name", accounts[0]["account_name"])
    else:
        set_setting("bank_name", "")
        set_setting("bank_account_number", "")
        set_setting("bank_account_name", "")


def get_bank_details() -> dict[str, str]:
    """Primary bank account details."""
    accounts = get_bank_accounts()
    if accounts:
        return accounts[0]
    return {
        "bank_name": (get_setting("bank_name", "") or os.environ.get("BANK_NAME", "OPay")).strip() or "OPay",
        "account_number": (get_setting("bank_account_number", "") or os.environ.get("BANK_ACCOUNT_NUMBER", "")).strip(),
        "account_name": (get_setting("bank_account_name", "") or os.environ.get("BANK_ACCOUNT_NAME", "CreditLog Store")).strip() or "CreditLog Store"
    }


def is_bank_configured() -> bool:
    """Check if bank transfer is set up."""
    return len(get_bank_accounts()) > 0


def get_bachs_key() -> str:
    """Bachs API key."""
    return (get_setting("bachs_api_key", "") or os.environ.get("BACHS_API_KEY", "")).strip()


def is_bachs_configured() -> bool:
    """Check if Bachs is set up."""
    key = get_bachs_key()
    return bool(key and not key.startswith("your_") and len(key) >= 10)


def get_bachs_base_url(api_key: Optional[str] = None) -> str:
    """Bachs API base URL."""
    custom_url = (get_setting("bachs_base_url", "") or os.environ.get("BACHS_BASE_URL", "")).strip()
    if custom_url:
        return custom_url.rstrip("/")
    key = (api_key or get_bachs_key()).lower()
    if "sandbox" in key or key.startswith("sk_test"):
        return "https://sandbox-api.bachs.io"
    return "https://api.bachs.io"


def get_bachs_currency() -> str:
    """Bachs billing currency (USD or NGN)."""
    curr = (get_setting("bachs_currency", "") or os.environ.get("BACHS_CURRENCY", "USD")).strip().upper()
    return curr if curr in ("USD", "NGN", "GHS", "KES", "ZAR", "EUR", "GBP") else "USD"


# Channel join check (force subscribe)
NUM_EMOJIS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]


def get_fsub_channels() -> List[str]:
    raw = get_setting("fsub_channels", REQUIRED_CHANNELS_RAW)
    if not raw:
        return []
    return [c.strip() for c in raw.split(",") if c.strip()]


def is_fsub_enabled() -> bool:
    channels = get_fsub_channels()
    if not channels:
        return False
    return get_setting("fsub_enabled", "1" if channels else "0") == "1"


def set_fsub_channels(channels: List[str]):
    set_setting("fsub_channels", ",".join(channels))


def set_fsub_enabled(enabled: bool):
    set_setting("fsub_enabled", "1" if enabled else "0")


def render_fsub_gate(channels: List[str]) -> Tuple[str, InlineKeyboardMarkup]:
    """Build join message and channel buttons."""
    lines = ["🔒 <b>Free join required for use bot, please join.</b>\n"]
    rows = []
    for i, ch in enumerate(channels):
        num_icon = NUM_EMOJIS[i] if i < len(NUM_EMOJIS) else f"{i+1}️⃣"
        name_clean = ch.strip()
        url = name_clean
        if not url.startswith("http"):
            username = name_clean.lstrip("@")
            url = f"https://t.me/{username}"

        is_group = "group" in name_clean.lower() or "chat" in name_clean.lower() or i == 1
        label_type = "Group" if is_group else "Channel"
        lines.append(f"{num_icon} <b>{label_type}:</b> {esc(name_clean)}")

        btn_title = "💬 Join Group" if is_group else "📢 Join Channel"
        rows.append([btn(btn_title, url=url, style="primary")])

    lines.append("\nAfter joining, tap <b>✅ Check Membership</b> below.")
    rows.append([btn("✅ Check Membership", "check_fsub", style="success")])
    return "\n".join(lines), kb(rows)


async def check_user_fsub(bot_inst: Bot, uid: int) -> Tuple[bool, List[str]]:
    """Check if user joined all required channels."""
    check_admin = get_setting("fsub_check_admin", "0") == "1"
    if uid in ADMIN_IDS and not check_admin:
        return True, []
    if not is_fsub_enabled():
        return True, []
    channels = get_fsub_channels()
    if not channels:
        return True, []

    missing = []
    for ch in channels:
        target = ch.strip()
        if not target:
            continue
        if target.startswith("https://t.me/"):
            target = "@" + target.replace("https://t.me/", "").rstrip("/")
        if not target.startswith("@") and not target.startswith("-100") and not target.lstrip("-").isdigit():
            target = f"@{target}"
        try:
            member = await bot_inst.get_chat_member(chat_id=target, user_id=uid)
            status = getattr(member, "status", None)
            if status in ("left", "kicked"):
                missing.append(ch)
        except Exception as e:
            err_str = str(e).lower()
            if "user not found" in err_str or "participant_id_invalid" in err_str or "user_not_participant" in err_str:
                missing.append(ch)
            elif "not enough rights" in err_str or "bot is not a member" in err_str or "chat not found" in err_str:
                log.warning("FSUB: Bot cannot check %s (%s). Please verify bot is admin in the chat.", ch, e)
            else:
                log.debug("FSUB check error for %s: %s", ch, e)
    return len(missing) == 0, missing


async def check_supplier_balance_sufficient(sup_client: ResellerClient, cost_usd: float) -> Tuple[bool, float]:
    """Check if supplier account balance covers order cost."""
    mode = get_setting("fulfillment_mode", "auto").lower()
    if mode in ("manual", "model_b", "b"):
        return False, 0.0
    if mode in ("instant", "model_a", "a"):
        return True, 999.0

    try:
        bal_info = await sup_client.get_balance()
        bal = float(bal_info.get("balance", 0.0))
        return (bal >= cost_usd and bal > 0.0), bal
    except Exception as e:
        log.warning("Could not check supplier balance: %s", e)
        return False, 0.0

# Store categories and icons
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

    cols_u = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "currency" not in cols_u:
        db.execute("ALTER TABLE users ADD COLUMN currency TEXT DEFAULT 'USD'")

    # Set categories for older products if missing
    for row in db.execute("SELECT id, name, category FROM products").fetchall():
        if not row["category"] or row["category"] == "AI Tools":
            detected = detect_category(row["name"])
            if detected != "AI Tools":
                db.execute("UPDATE products SET category=? WHERE id=?", (detected, row["id"]))

    # Default showcase products
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

    # Initial product catalog
    MANUAL_CATALOG_SEEDS = [
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
        ("ChatGPT Plus 1M Momo Pay", "ChatGPT Plus 1 Month Momo Pay access.", 600, "ChatGPT"),
        ("ChatGPT Plus 1M 30D", "ChatGPT Plus 1 Month 30 Days account.", 768, "ChatGPT"),
        ("ChatGPT Plus 1M Pix", "ChatGPT Plus 1 Month Pix payment access.", 590, "ChatGPT"),
        ("ChatGPT Plus Your Mail 1M", "ChatGPT Plus 1 Month upgraded on your email.", 1800, "ChatGPT"),

        ("ChatGPT Business 1M", "ChatGPT Business Workspace 1 Month.", 650, "ChatGPT"),
        ("ChatGPT Plus 4M Ready Account", "ChatGPT Plus 4 Months ready-made account.", 5750, "ChatGPT"),
        ("Admin Netflix 4K Premium 1M 5 Profiles", "Netflix 4K Premium 1 Month 5 Profiles Admin.", 250, "Streaming"),
        ("Admin Netflix 4K 1M", "Netflix 4K Admin 1 Month access.", 380, "Streaming"),
        ("ExpressVPN 1 Month Private", "ExpressVPN 1 Month Private dedicated slot.", 350, "VPN"),
        ("Codex API 500M Tokens 6D", "Codex API 500M tokens 6 Days access.", 3400, "Codex API"),
        ("Apple Music 5M", "Apple Music 5 Months subscription.", 200, "Music"),
        ("Apple TV+ 12 Months", "Apple TV+ 12 Months full plan.", 600, "Streaming"),
        ("Capcut 6M Personal", "CapCut Pro 6 Months personal account.", 1635, "Design"),
        ("Gamma Pro 1 Year", "Gamma Pro AI presentation maker 1 Year.", 2900, "AI Tools"),
        ("ElevenLabs Creator 1 Year", "ElevenLabs Creator 1 Year plan.", 3700, "ElevenLabs"),
        ("ChatGPT Plus 1M Google Pay", "ChatGPT Plus 1 Month Google Pay.", 870, "ChatGPT"),
        ("ChatGPT Plus 1M GPAY", "ChatGPT Plus 1 Month GPAY.", 830, "ChatGPT"),
        ("ChatGPT Plus 1M UPI", "ChatGPT Plus 1 Month UPI account.", 600, "ChatGPT"),
        ("Kling 66 Credits", "Kling AI 66 Credits pack.", 55, "Video AI"),

        ("Gmail account new", "Gmail freshly created PVA account.", 62, "Office"),
        ("HBO Max Standard 3 Months", "HBO Max Standard 3 Months private slot.", 200, "Streaming"),
        ("HBO Max Standard 5 Months", "HBO Max Standard 5 Months private slot.", 250, "Streaming"),
        ("CapCut Pro 7D", "CapCut Pro 7 Days quick access.", 50, "Design"),
        ("CapCut Pro 7 Days", "CapCut Pro 7 Days standard license.", 65, "Design"),
        ("Adobe Full App Bypass Version 1M", "Adobe Full App Bypass Version 1 Month.", 550, "Adobe"),
        ("Loveable Pro 1M Redeem Link", "Loveable Pro 1 Month official redeem link.", 120, "AI Tools"),
        ("Windows 10 Pro Retail Key", "Windows 10 Pro Retail digital license key.", 300, "Office"),
        ("Duolingo Super 2M", "Duolingo Super 2 Months subscription.", 50, "Education"),
        ("Notion Business 1 Year", "Notion Business Workspace 1 Year.", 1250, "Office"),
        ("Autodesk Education Plan 1 Year", "Autodesk Education Plan 1 Year access.", 200, "Autodesk"),
        ("YouTube Ready Account 3M", "YouTube Premium Ready Account 3 Months.", 220, "Streaming"),
        ("JetBrains EDU 1 Year", "JetBrains All Products EDU 1 Year.", 450, "Office"),
        ("Higgsfield Starter 1M", "Higgsfield AI Video Starter 1 Month.", 1000, "Video AI"),
        ("OpenArt Starter 4K Credits 1M", "OpenArt Starter 4K Credits 1 Month.", 450, "AI Tools"),

        ("Coursera Premium 12 Months", "Coursera Plus/Premium 12 Months learning.", 350, "Education"),
        ("Spotify Premium 3M", "Spotify Premium 3 Months private account.", 150, "Music"),
        ("Kiro Pro Edu Official Owner 1Y", "Kiro Pro EDU Official Owner 1 Year.", 7300, "Education"),
        ("Spotify Individual 12M", "Spotify Individual 12 Months subscription.", 2200, "Music"),
        ("Spotify Premium 6 Months", "Spotify Premium 6 Months subscription.", 980, "Music"),
        ("Claude API 50M Tokens 3 Days", "Claude API 50M tokens valid 3 days.", 600, "Claude"),
        ("Apple TV+ 6 Months", "Apple TV+ 6 Months subscription.", 450, "Streaming"),
        ("Amazon Prime Video 1 Month", "Amazon Prime Video 1 Month private slot.", 200, "Streaming"),
        ("QuillBot Premium 1M", "QuillBot Premium 1 Month paraphrase tool.", 300, "Office"),
        ("Snapchat Plus+ 3 Months", "Snapchat Plus 3 Months access.", 500, "Social"),
        ("Peacock Official Subscription 1 Year", "Peacock Official TV 1 Year subscription.", 500, "Streaming"),
        ("Gamma AI 20K Credits 10 Years", "Gamma AI 20K Credits 10 Years lifetime.", 1100, "AI Tools"),
        ("Manus Pro 12 Months", "Manus Pro AI agent 12 Months access.", 3500, "AI Tools"),
        ("KlingAI 750-1100 Credits 1M", "KlingAI 750-1100 Credits 1 Month video plan.", 1138, "Video AI"),
        ("Kling Random 700-1000 Credits 1M", "Kling Random 700-1000 Credits 1 Month video plan.", 850, "Video AI"),

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
        retail = max(100, round(m_price * (1 + PROFIT_MARGIN / 100.0)))
        exists = db.execute("SELECT id, margin, price, supplier_price FROM products WHERE name=?", (m_name,)).fetchone()
        if not exists:
            db.execute("""
                INSERT INTO products(name, description, price, supplier_price, category, auto_fulfill, active, supplier_stock, delivery_type)
                VALUES(?,?,?,?,?,0,1,?,'manual')
            """, (m_name, m_desc, retail, m_price, m_cat, m_stock))
        else:
            # Update retail price if missing or set at cost
            if exists["supplier_price"] is None or exists["price"] == m_price:
                db.execute("""
                    UPDATE products SET price=?, supplier_price=? WHERE id=?
                """, (retail, m_price, exists["id"]))
            db.execute("""
                UPDATE products SET category=?, description=COALESCE(description, ?), supplier_stock=? WHERE id=?
            """, (m_cat, m_desc, m_stock, exists["id"]))

    # Add markup to any products saved at cost price
    db.execute("""
        UPDATE products
        SET price = max(100, round(supplier_price * (1 + ? / 100.0)))
        WHERE supplier_price IS NOT NULL AND price = supplier_price
    """, (PROFIT_MARGIN,))

    # Keep manual items available even if stock is 0
    db.execute("UPDATE products SET supplier_stock=NULL WHERE delivery_type='manual' AND supplier_stock=0")

    # Brand custom emoji table
    db.execute("""
        CREATE TABLE IF NOT EXISTS custom_emojis(
          key TEXT PRIMARY KEY,
          emoji_id TEXT NOT NULL,
          created TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

migrate_db()


def get_custom_emoji_id(key: str) -> Optional[str]:
    """Get custom emoji ID for a brand or button from DB or environment."""
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

# Active user input sessions (quantity, search, txid)
pending_inputs: dict[int, dict] = {}

# Translations
TR = {
    "en": {
        "welcome": "🛍 <b>Welcome to Gemini Pro Store</b>\n\n💵 Best prices\n⚡ Fast delivery after payment\n📦 New products and restocks\n💬 Can't find what you need? Contact support\n\nChoose a section below:",
        "btn_shop": "🛍 Shop",
        "btn_orders": "📦 Order",
        "btn_lang": "🌐 Language",
        "btn_currency": "💱 Currency",
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
        "currency_choose": "💱 <b>Choose your currency:</b>\nPrices throughout the catalog will be displayed in your selected currency.",
        "currency_set": "Currency updated to {currency}.",
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
        "btn_currency": "💱 Moneda",
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
        "currency_choose": "💱 <b>Elige tu moneda:</b>\nLos precios se mostrarán en la moneda seleccionada.",
        "currency_set": "Moneda actualizada a {currency}.",
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
        "btn_currency": "💱 Валюта",
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
        "currency_choose": "💱 <b>Выберите валюту:</b>\nЦены в магазине будут отображаться в выбранной валюте.",
        "currency_set": "Валюта обновлена на {currency}.",
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
        "btn_currency": "💱 العملة",
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
        "currency_choose": "💱 <b>اختر العملة:</b>\nسيتم عرض الأسعار في البوت بالعملة المختارة.",
        "currency_set": "تم تحديث العملة إلى {currency}.",
        "stock_auto": "تسليم فوري وتلقائي",
        "supplier_error": "⚠️ المنتج غير متوفر مؤقتًا لدى المورد. يرجى المحاولة لاحقًا أو التواصل مع الدعم.",
        "checkout_unavailable": "الدفع غير متاح مؤقتًا. يرجى التواصل مع الدعم.",
        "out_of_stock": "نفدت الكمية أو غير متوفر.",
    },
}
LANG_NAMES = {"en": "English", "es": "Español", "ru": "Русский", "ar": "العربية"}
CURRENCY_NAMES = {
    "USD": "USD ($)",
    "NGN": "NGN (₦)",
}

# Helpers & UI
def get_user(uid: int, lang_code: str | None = None):
    try:
        row = db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        if row:
            return row
        lang = (lang_code or "en")[:2]
        lang = lang if lang in TR else "en"
        try:
            db.execute("INSERT INTO users(id, lang, currency) VALUES(?,?, 'USD')", (uid, lang))
        except sqlite3.OperationalError:
            try:
                db.execute("ALTER TABLE users ADD COLUMN currency TEXT DEFAULT 'USD'")
                db.execute("INSERT INTO users(id, lang, currency) VALUES(?,?, 'USD')", (uid, lang))
            except sqlite3.OperationalError:
                db.execute("INSERT INTO users(id, lang) VALUES(?,?)", (uid, lang))
        return db.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    except Exception as e:
        log.error("get_user error: %s", e)
        return {"id": uid, "lang": "en", "currency": "USD"}


def get_user_currency(uid: int) -> str:
    try:
        if isinstance(uid, int):
            user = get_user(uid)
            if user and "currency" in user.keys() and user["currency"]:
                curr = str(user["currency"]).upper()
                return curr if curr in CURRENCY_NAMES else "USD"
    except Exception:
        pass
    return "USD"


def format_price(cents: Optional[int], currency: str = "USD") -> str:
    try:
        val = float(cents) if cents is not None else 0.0
    except (ValueError, TypeError):
        val = 0.0
    if currency == "NGN":
        rate = get_usd_ngn_rate()
        ngn_val = round((val / 100.0) * rate)
        return f"₦{ngn_val:,}"
    return usd(val)


def format_user_price(cents: int, uid: int) -> str:
    return format_price(cents, get_user_currency(uid))


def parse_currency_amount(raw_amount: str, default_curr: str = "USD") -> Tuple[int, str, float]:
    """
    Parses a user or admin provided price string in any currency (USD or NGN).
    Supports:
      - '$12.50', '12.50 USD', '12.50', '$12' -> USD ($12.50 -> 1250 cents)
      - '₦15,000', '15000 NGN', '15,000 NGN', 'N15000', '15000 NAIRA' -> NGN (converted to USD cents via get_usd_ngn_rate())
      - If no currency symbol or code is provided:
          - If default_curr == 'NGN': treated as NGN value and converted to USD cents.
          - Otherwise: treated as USD amount.
    Returns:
      (cents: int, currency: str, original_val: float)
    Raises:
      ValueError if amount is invalid or <= 0
    """
    if not raw_amount or not str(raw_amount).strip():
        raise ValueError("Price amount cannot be empty.")
    s = str(raw_amount).strip()

    # Detect currency
    upper = s.upper()
    if "₦" in upper or "NGN" in upper or "NAIRA" in upper:
        curr = "NGN"
    elif "$" in upper or "USD" in upper:
        curr = "USD"
    elif upper.startswith("N") and len(upper) > 1 and (upper[1].isdigit() or upper[1] in " ,."):
        curr = "NGN"
    else:
        curr = (default_curr or "USD").upper()
        if curr not in ("USD", "NGN"):
            curr = "USD"

    # Strip non-numbers
    cleaned = (
        upper.replace("USD", "")
        .replace("NGN", "")
        .replace("NAIRA", "")
        .replace("$", "")
        .replace("₦", "")
        .replace(",", "")
        .strip()
    )
    if cleaned.startswith("N"):
        cleaned = cleaned[1:].strip()

    try:
        val = float(cleaned)
    except (ValueError, TypeError):
        raise ValueError(f"Could not parse numeric price from '{raw_amount}'.")

    if val <= 0:
        raise ValueError("Price must be greater than 0.")

    if curr == "NGN":
        rate = get_usd_ngn_rate()
        cents = max(1, round((val / rate) * 100))
        return (cents, "NGN", val)
    else:
        cents = max(1, round(val * 100))
        return (cents, "USD", val)


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
    """Check if product is active and in stock."""
    if not p["active"]:
        return False
    is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
    deliv_type = p["delivery_type"] if ("delivery_type" in p.keys() and p["delivery_type"]) else ("auto" if is_auto else "manual")
    sup_stock = p["supplier_stock"] if "supplier_stock" in p.keys() else None
    count = stock_count(p["id"]) if not is_auto else None

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
        err = str(e).lower()
        if "not modified" in err:
            pass
        elif any(phrase in err for phrase in ("message to edit not found", "message can't be edited", "message is not modified")):
            try:
                await c.message.answer(text, reply_markup=markup)
            except Exception:
                pass
        else:
            raise
    except Exception:
        try:
            await c.message.answer(text, reply_markup=markup)
        except Exception:
            pass
    try:
        await c.answer()
    except Exception:
        pass


async def notify_admins(bot: Bot, text_or_fn):
    for aid in ADMIN_IDS:
        try:
            msg = text_or_fn(aid) if callable(text_or_fn) else text_or_fn
            await bot.send_message(aid, msg)
        except Exception:
            pass


def home_kb(uid: int):
    """Home screen buttons."""
    rows = []

    # Featured product slot 1
    gemini_p = db.execute(
        "SELECT * FROM products WHERE name LIKE '%Gemini 18 Months%' AND active=1 ORDER BY id LIMIT 1"
    ).fetchone()
    if not gemini_p:
        gemini_p = db.execute(
            "SELECT * FROM products WHERE category='Gemini' AND active=1 ORDER BY id LIMIT 1"
        ).fetchone() or db.execute("SELECT * FROM products WHERE active=1 ORDER BY id LIMIT 1").fetchone()

    # Featured product slot 2
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
        text_g = f"{gemini_p['name']} · {format_user_price(gemini_p['price'], uid)}" if gemini_emoji_id else f"1️⃣ {gemini_p['name']} · {format_user_price(gemini_p['price'], uid)}"
        rows.append([btn(text_g, f"p:{gemini_p['id']}:home", style="success", emoji_id=gemini_emoji_id)])

    if duo_p:
        badge = "🦉" if "duolingo" in duo_p["name"].lower() else "2️⃣"
        text_d = f"{duo_p['name']} · {format_user_price(duo_p['price'], uid)}" if duo_emoji_id else f"{badge} {duo_p['name']} · {format_user_price(duo_p['price'], uid)}"
        rows.append([btn(text_d, f"p:{duo_p['id']}:home", style="success", emoji_id=duo_emoji_id)])

    shop_emoji_id = get_custom_emoji_id("shop")
    orders_emoji_id = get_custom_emoji_id("orders") or get_custom_emoji_id("purchases")
    lang_emoji_id = get_custom_emoji_id("lang") or get_custom_emoji_id("language")
    support_emoji_id = get_custom_emoji_id("support")
    news_emoji_id = get_custom_emoji_id("news") or get_custom_emoji_id("channel")
    currency_emoji_id = get_custom_emoji_id("currency")

    rows += [
        [btn(t(uid, "btn_shop"), "shop", style="success", emoji_id=shop_emoji_id),
         btn(t(uid, "btn_orders"), "orders", style="success", emoji_id=orders_emoji_id)],
        [btn(t(uid, "btn_lang"), "lang", style="primary", emoji_id=lang_emoji_id),
         btn(t(uid, "btn_support"), url=SUPPORT_URL, style="primary", emoji_id=support_emoji_id)],
        [btn(t(uid, "btn_news"), url=CHANNEL_URL, style="primary", emoji_id=news_emoji_id),
         btn(t(uid, "btn_currency"), "currency", style="primary", emoji_id=currency_emoji_id)],
    ]
    return kb(rows)


def categories_kb(uid: int):
    """Category grid buttons."""
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

        p_price_disp = format_user_price(p['price'], uid)
        btn_text = f"{p['name']} · {p_price_disp} {status_mark}" if p_emoji_id else f"{icon} {p['name']} · {p_price_disp} {status_mark}"
        rows.append([btn(btn_text, f"p:{p['id']}:{ctx_prefix}_{avail_only}_{page}", style=btn_style, emoji_id=p_emoji_id)])

    if total_pages > 1:
        pag_row = []
        pag_row.append(btn("◀️", f"{cb_prefix}:{page - 1}:{avail_only}" if page > 1 else "noop", style="primary"))
        pag_row.append(btn(f"{page}/{total_pages}", "noop", style="primary"))
        pag_row.append(btn("▶️", f"{cb_prefix}:{page + 1}:{avail_only}" if page < total_pages else "noop", style="primary"))
        rows.append(pag_row)

    # Toggle available items filter
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
    """Product card with quantity buttons."""
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

    curr = get_user_currency(uid)
    if curr == "NGN":
        rate = get_usd_ngn_rate()
        ngn_val = round((p["price"] / 100.0) * rate)
        price_disp = f"₦{ngn_val:,}"
    else:
        price_disp = f"{p['price'] / 100:.2f} USD"

    desc = (p["description"] or "").strip()
    if desc:
        text = (
            f"Item: <b>{esc(p['name'])}</b>\n"
            f"📝 Description: {esc(desc)}\n\n"
            f"💵 Price: {price_disp}\n"
            f"{stock_line}"
        )
    else:
        text = (
            f"Item: <b>{esc(p['name'])}</b>\n\n"
            f"💵 Price: {price_disp}\n"
            f"{stock_line}"
        )

    rows = []
    if has_stock:
        qty_row = []
        qty_row.append(btn("1", f"buyqty:{p['id']}:1:{ctx}", style="success"))
        if is_auto or deliv_type == "manual":
            qty_row.append(btn("5", f"buyqty:{p['id']}:5:{ctx}", style="success"))
            qty_row.append(btn("10", f"buyqty:{p['id']}:10:{ctx}", style="success"))
        else:
            if count and count >= 5:
                qty_row.append(btn("5", f"buyqty:{p['id']}:5:{ctx}", style="success"))
                qty_row.append(btn(f"All {count}", f"buyqty:{p['id']}:{count}:{ctx}", style="success"))
            elif count and count > 1:
                qty_row.append(btn(f"All {count}", f"buyqty:{p['id']}:{count}:{ctx}", style="success"))
        rows.append(qty_row)
        rows.append([btn(t(uid, "btn_custom_qty"), f"cqty:{p['id']}:{ctx}", style="primary")])

    # Back button target
    if ctx == "home":
        back_cb = "home"
    elif ctx.startswith("c_"):
        raw = ctx[2:]
        rparts = raw.rsplit("_", 2)
        if len(rparts) == 3:
            cat_key, avail_only, page = rparts[0], rparts[1], rparts[2]
            back_cb = f"cat:{cat_key}:{page}:{avail_only}"
        elif len(rparts) == 2:
            cat_key, page = rparts[0], rparts[1]
            back_cb = f"cat:{cat_key}:{page}:0"
        else:
            back_cb = f"cat:{raw}:1:0"
    elif ctx.startswith("all_"):
        raw = ctx[4:]
        rparts = raw.rsplit("_", 2)
        if len(rparts) == 2:
            avail_only, page = rparts[0], rparts[1]
            back_cb = f"allprods:{page}:{avail_only}"
        elif len(rparts) == 1:
            back_cb = f"allprods:{rparts[0]}:0"
        else:
            back_cb = "shop"
    elif ctx.startswith("s_"):
        raw = ctx[2:]
        rparts = raw.rsplit("_", 2)
        if len(rparts) == 3:
            term, avail_only, page = rparts[0], rparts[1], rparts[2]
            back_cb = f"srch:{term}:{page}:{avail_only}"
        elif len(rparts) == 2:
            term, page = rparts[0], rparts[1]
            back_cb = f"srch:{term}:{page}:0"
        else:
            back_cb = f"srch:{raw}:1:0"
    else:
        back_cb = "shop"

    rows.append([
        btn(t(uid, "btn_refresh"), f"p:{p['id']}:{ctx}", style="primary"),
        btn(t(uid, "btn_back"), back_cb, style="danger")
    ])
    return text, kb(rows)


# Order fulfillment
async def fulfill_order(bot: Bot, oid: int, force_supplier: bool = False) -> Tuple[Optional[str], Optional[str]]:
    """
    Fulfills an order:
      - Model A (when top-up balance > 0): Calls supplier API POST /orders immediately and auto-delivers.
      - Model B (when top-up balance is insufficient / empty): Pauses automated delivery and notifies admin to
        use customer's payment to top up, then tap /approve_<oid>.
      - If local stock: pulls available stock items from database.
    Returns (payload, error_key).
    """
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return None, "out_of_stock"
    if o["status"] == "completed":
        return o["payload"] or "COMPLETED", None
    if o["refunded"] or o["status"] in ("refunded", "cancelled", "rejected"):
        return None, "order_cancelled"

    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (o["product_id"],)).fetchone()
    if not p:
        return None, "out_of_stock"

    qty = o["quantity"] if ("quantity" in o.keys() and o["quantity"] and o["quantity"] > 0) else 1

    # 1. Auto-fulfill from supplier
    if p["auto_fulfill"] and p["supplier_product_id"]:
        sup_name = p["supplier"] if ("supplier" in p.keys() and p["supplier"]) else "default"
        sup_client = supplier_registry.get(sup_name) or reseller

        unit_cost = (p["supplier_price"] / 100.0) if ("supplier_price" in p.keys() and p["supplier_price"]) else (p["price"] * 0.65 / 100.0)
        total_cost = unit_cost * qty

        # Check if supplier balance covers wholesale cost
        if not force_supplier:
            has_bal, cur_bal = await check_supplier_balance_sufficient(sup_client, total_cost)
            if not has_bal:
                # Supplier balance is low - hold for admin approval
                db.execute("UPDATE orders SET status='manual_pending' WHERE id=?", (oid,))
                deficit = max(0.0, total_cost - cur_bal)
                bal_status = f"${cur_bal:.2f} USDT (Short by ${deficit:.2f} USDT)" if cur_bal > 0 else f"${cur_bal:.2f} USDT (Empty)"
                topup_needed = f"at least <b>${deficit:.2f} USDT</b> (shortfall)" if cur_bal > 0 else f"<b>${total_cost:.2f} USDT</b>"
                await notify_admins(
                    bot,
                    f"🟡 <b>Model B Active (Supplier Balance Insufficient) — Order #{oid}</b>\n"
                    f"• <b>Product:</b> {esc(p['name'])} x{qty}\n"
                    f"• <b>Customer Paid:</b> {usd(o['price'])} USDT (in your wallet)\n"
                    f"• <b>Wholesale Cost:</b> ${total_cost:.2f} USDT\n"
                    f"• <b>Supplier Balance:</b> {bal_status}\n\n"
                    f"⚡ <b>Next Step (Customer Money Top-Up):</b>\n"
                    f"1. The customer's payment is already confirmed and safe in your wallet.\n"
                    f"2. Top up your supplier account with {topup_needed}.\n"
                    f"3. Send <code>/approve_{oid}</code> to buy & deliver automatically to the customer.\n"
                    f"   <i>(Or send <code>/deliver {oid} &lt;item&gt;</code> to deliver manually)</i>"
                )
                return "MANUAL_PENDING", None

        # Place order with supplier
        try:
            order_res = await sup_client.create_order(product_id=p["supplier_product_id"], quantity=qty)
            payload = order_res.get("value")
            supplier_order_id = order_res.get("order_code")
            if not payload:
                raise ResellerAPIError(f"Supplier order succeeded but returned empty value: {order_res}")
        except Exception as e:
            log.exception("Supplier order failed for order %s (supplier: %s)", oid, sup_name)
            db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
            await notify_admins(
                bot,
                f"⚠️ Supplier order failed for order #{oid} (user {o['user_id']}, product #{p['id']}, supplier '{sup_name}', qty {qty}):\n"
                f"<code>{esc(str(e))}</code>\n\n"
                f"👉 Top up your supplier balance, then retry with <code>/approve_{oid}</code> or deliver with <code>/deliver {oid} &lt;item&gt;</code>."
            )
            return None, "supplier_error"

        db.execute(
            "UPDATE orders SET payload=?, supplier_order_id=?, supplier=?, status='completed' WHERE id=?",
            (payload, supplier_order_id, sup_name, oid)
        )
        return payload, None

    # 2. Deliver from local stock
    try:
        db.execute("BEGIN IMMEDIATE")
        s_items = db.execute(
            "SELECT * FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT ?",
            (p["id"], qty)
        ).fetchall()
        if s_items and len(s_items) >= qty:
            ids = [item["id"] for item in s_items]
            db.execute(f"UPDATE stock SET sold=1 WHERE id IN ({','.join(['?']*len(ids))})", ids)
            payload = "\n".join(item["payload"] for item in s_items)
            db.execute("UPDATE orders SET payload=?, status='completed' WHERE id=?", (payload, oid))
            db.execute("COMMIT")
            return payload, None
        else:
            db.execute("ROLLBACK")
    except Exception:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise

    # 3. Manual delivery
    deliv = p["delivery_type"] if ("delivery_type" in p.keys() and p["delivery_type"]) else "auto"
    if deliv == "manual" or not p["auto_fulfill"]:
        db.execute("UPDATE orders SET status='manual_pending' WHERE id=?", (oid,))
        await notify_admins(
            bot,
            lambda aid: (
                f"🔔 <b>New Manual Order #{oid}</b>\n"
                f"• Item: <b>{esc(p['name'])}</b> (Qty: {qty})\n"
                f"• User ID: <code>{o['user_id']}</code>\n"
                f"• Amount: <b>{format_user_price(o['price'], aid)}</b>\n\n"
                f"👉 Use <code>/deliver {oid} &lt;credentials/link&gt;</code> to send item to user."
            )
        )
        return "MANUAL_PENDING", None

    db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
    return None, "out_of_stock"


# User handlers
@dp.message(CommandStart())
@dp.message(Command("start"))
@dp.message(Command("menu"))
async def start(m: Message):
    pending_inputs.pop(m.from_user.id, None)
    uid = m.from_user.id
    get_user(uid, m.from_user.language_code)
    passed, missing = await check_user_fsub(m.bot, uid)
    if not passed:
        text, markup = render_fsub_gate(missing)
        return await m.answer(text, reply_markup=markup)
    await m.answer(t(uid, "welcome"), reply_markup=home_kb(uid))


@dp.message(Command("shop"))
async def cmd_shop(m: Message):
    pending_inputs.pop(m.from_user.id, None)
    uid = m.from_user.id
    get_user(uid, m.from_user.language_code)
    passed, missing = await check_user_fsub(m.bot, uid)
    if not passed:
        text, markup = render_fsub_gate(missing)
        return await m.answer(text, reply_markup=markup)
    await m.answer(t(uid, "cat_title"), reply_markup=categories_kb(uid))


@dp.callback_query(F.data == "home")
async def home(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    passed, missing = await check_user_fsub(c.bot, uid)
    if not passed:
        text, markup = render_fsub_gate(missing)
        return await edit(c, text, markup)
    await edit(c, t(uid, "welcome"), home_kb(uid))


@dp.callback_query(F.data == "shop")
async def shop(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    passed, missing = await check_user_fsub(c.bot, uid)
    if not passed:
        text, markup = render_fsub_gate(missing)
        return await edit(c, text, markup)
    await edit(c, t(uid, "cat_title"), categories_kb(uid))


@dp.callback_query(F.data == "check_fsub")
async def on_check_fsub(c: CallbackQuery):
    uid = c.from_user.id
    passed, missing = await check_user_fsub(c.bot, uid)
    if passed:
        await c.answer("✅ Membership verified! Welcome to the store.", show_alert=False)
        pending_inputs.pop(uid, None)
        get_user(uid, c.from_user.language_code)
        await edit(c, t(uid, "welcome"), home_kb(uid))
    else:
        await c.answer("⚠️ You have not joined all required channels/groups yet. Please join and try again!", show_alert=True)
        text, markup = render_fsub_gate(missing)
        try:
            await edit(c, text, markup)
        except Exception:
            pass


@dp.callback_query(F.data.startswith("cat:"))
async def on_category(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    passed, missing = await check_user_fsub(c.bot, uid)
    if not passed:
        text, markup = render_fsub_gate(missing)
        return await edit(c, text, markup)
    parts = c.data.split(":")
    if len(parts) < 2 or not parts[1]:
        return await c.answer("⚠️ Invalid category callback data.", show_alert=True)
    key = parts[1]
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    avail_only = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
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
    page = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
    avail_only = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
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
    term = parts[1] if len(parts) > 1 else ""
    page = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 1
    avail_only = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else 0
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
    if len(parts) < 2 or not parts[1].isdigit():
        return await c.answer("⚠️ Invalid product callback data.", show_alert=True)
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
    """Create a CryptoBot invoice for an order."""
    if not CRYPTOBOT_TOKEN:
        raise RuntimeError("CryptoBot token is not configured.")
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
            try:
                data = await r.json()
            except Exception:
                text_resp = await r.text()
                raise RuntimeError(f"CryptoBot HTTP {r.status}: {text_resp[:200]}")
            if not data.get("ok"):
                raise RuntimeError(f"CryptoBot error: {data}")
            return data["result"]


async def check_cryptobot_invoice(invoice_id: int) -> bool:
    """Check if a @CryptoBot invoice is paid."""
    if not CRYPTOBOT_TOKEN:
        return False
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(
                f"https://pay.crypt.bot/api/getInvoices?invoice_ids={invoice_id}",
                headers={"Crypto-Pay-API-Token": CRYPTOBOT_TOKEN}
            ) as r:
                try:
                    data = await r.json()
                except Exception:
                    return False
                if data.get("ok") and data.get("result", {}).get("items"):
                    items = data["result"]["items"]
                    return items[0].get("status") == "paid"
                return False
    except Exception as e:
        log.warning("CryptoBot getInvoices check failed: %s", e)
        return False


async def initialize_bachs_transaction(
    order_id: int,
    amount_cents: int,
    product_name: str,
    customer_email: Optional[str] = None,
    uid: Optional[int] = None,
    qty: int = 1
) -> Tuple[bool, dict]:
    """Initialize a Bachs checkout session for an order."""
    key = get_bachs_key()
    if not key:
        return False, {"error": "Bachs API key is not configured."}

    base_url = get_bachs_base_url(key)
    currency = get_bachs_currency()

    if currency == "NGN":
        rate = get_usd_ngn_rate()
        amount_val = round((amount_cents / 100.0) * rate)
        amount_str = f"{amount_val:.2f}"
    else:
        amount_val = amount_cents / 100.0
        amount_str = f"{amount_val:.2f}"

    callback_url = (get_setting("bachs_callback_url", "") or os.environ.get("BACHS_CALLBACK_URL", "")).strip()
    bot_username = (
        get_setting("bot_username", "")
        or os.environ.get("BOT_USERNAME", "")
        or "creditlog_bot"
    ).strip().lstrip("@")

    tg_url = f"https://t.me/{bot_username}?start=order_{order_id}"
    success_url = callback_url or tg_url
    cancel_url = callback_url or tg_url

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }
    payload = {
        "pricing": {
            "amount": amount_str,
            "currency": currency
        },
        "metadata": {
            "order_id": str(order_id),
            "user_id": str(uid or ""),
            "product_name": str(product_name)[:100],
            "quantity": str(qty)
        },
        "success_url": success_url,
        "cancel_url": cancel_url
    }
    if customer_email:
        payload["customer"] = {"email": customer_email}

    timeout = aiohttp.ClientTimeout(total=20)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.post(f"{base_url}/v1/checkout-sessions", json=payload, headers=headers) as r:
                try:
                    data = await r.json()
                except Exception:
                    raw_text = await r.text()
                    return False, {"error": f"Bachs returned non-JSON response (HTTP {r.status}): {raw_text[:200]}"}

                if r.status in (200, 201):
                    sess_data = data.get("data") if isinstance(data.get("data"), dict) else data
                    checkout_id = sess_data.get("checkout_id") or sess_data.get("id")
                    checkout_url = sess_data.get("checkout_url") or sess_data.get("url")
                    if checkout_id and checkout_url:
                        return True, {
                            "checkout_id": checkout_id,
                            "checkout_url": checkout_url,
                            "status": sess_data.get("status", "open"),
                            "amount": sess_data.get("amount") or amount_str,
                            "currency": currency,
                            "raw": data
                        }
                    return False, {"error": "Missing checkout_id or checkout_url in Bachs response", "raw": data}
                else:
                    err_msg = data.get("message") or data.get("error") or str(data)
                    return False, {"error": err_msg, "status_code": r.status, "raw": data}
    except Exception as e:
        log.exception("Bachs initialize transaction failed: %s", e)
        return False, {"error": str(e)}


async def verify_bachs_transaction(checkout_id: str) -> Tuple[bool, dict]:
    """Retrieve and verify a Bachs checkout session."""
    key = get_bachs_key()
    if not key:
        return False, {"error": "Bachs API key is not configured."}

    base_url = get_bachs_base_url(key)
    headers = {
        "Authorization": f"Bearer {key}",
        "Accept": "application/json"
    }
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as s:
            async with s.get(f"{base_url}/v1/checkout-sessions/{checkout_id}", headers=headers) as r:
                try:
                    data = await r.json()
                except Exception:
                    raw_text = await r.text()
                    return False, {"error": f"Bachs returned non-JSON response (HTTP {r.status}): {raw_text[:200]}"}

                if r.status == 200:
                    sess_data = data.get("data") if isinstance(data.get("data"), dict) else data
                    status = str(sess_data.get("status") or "").lower()
                    is_completed = status in ("completed", "paid", "success", "succeeded")

                    amount = sess_data.get("amount")
                    if amount is None and isinstance(sess_data.get("pricing"), dict):
                        amount = sess_data["pricing"].get("amount")

                    currency = sess_data.get("currency")
                    if currency is None and isinstance(sess_data.get("pricing"), dict):
                        currency = sess_data["pricing"].get("currency")

                    return is_completed, {
                        "checkout_id": checkout_id,
                        "status": status,
                        "amount": amount,
                        "currency": currency,
                        "raw": data
                    }
                else:
                    err_msg = data.get("message") or data.get("error") or str(data)
                    return False, {"error": err_msg, "status_code": r.status, "raw": data}
    except Exception as e:
        log.exception("Bachs verification failed for %s: %s", checkout_id, e)
        return False, {"error": str(e)}



def render_order_summary(uid: int, p: sqlite3.Row, qty: int, ctx: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Order summary before payment."""
    total_cents = p["price"] * qty
    icon = get_product_icon(p["name"], p["category"] if "category" in p.keys() else None)
    unit_usd = f"{p['price'] / 100:.2f}"
    total_usd = f"{total_cents / 100:.2f}"

    curr = get_user_currency(uid)
    if curr == "NGN":
        rate = get_usd_ngn_rate()
        unit_ngn = round((p["price"] / 100.0) * rate)
        total_ngn = round((total_cents / 100.0) * rate)
        price_lines = (
            f"🤑 <b>Price:</b> ₦{unit_ngn:,}\n"
            f"🪙 <b>Total:</b> ₦{total_ngn:,}\n\n"
        )
    else:
        price_lines = (
            f"🤑 <b>Price:</b> ${unit_usd} each\n"
            f"🪙 <b>Total:</b> ${total_usd} USDT\n\n"
        )

    text = (
        "🧾 <b>Order Summary</b>\n\n"
        f"🥇 {icon} <b>{esc(p['name'])}</b>\n"
        f"🔢 <b>Qty:</b> {qty}\n"
        f"{price_lines}"
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
    """Payment method options screen."""
    _ = uid
    pid = p["id"]
    cb_emoji = get_custom_emoji_id("cryptopay") or get_custom_emoji_id("cryptobot")
    binance_emoji = get_custom_emoji_id("binance") or get_custom_emoji_id("binance_pay")
    usdt_emoji = get_custom_emoji_id("usdt") or get_custom_emoji_id("tether")
    card_emoji = get_custom_emoji_id("card") or get_custom_emoji_id("fiat")
    back_emoji = get_custom_emoji_id("back")

    cb_label = "CryptoPay (Crypto Bot) +3%" if cb_emoji else "🤖 CryptoPay (Crypto Bot) +3%"
    binance_label = "Binance Pay (USDT only)" if binance_emoji else "🟡 Binance Pay (USDT only)"
    usdt_label = "USDT (TRC20 / BEP20)" if usdt_emoji else "🟢 USDT (TRC20 / BEP20)"
    card_label = "Don't have crypto?" if card_emoji else "💳 Don't have crypto?"
    back_label = "Back" if back_emoji else "↩️ Back"

    rows = [
        [btn(cb_label, f"pay_opt:cryptobot:{pid}:{qty}:{ctx}", style="success", emoji_id=cb_emoji)],
        [btn(binance_label, f"pay_opt:binance:{pid}:{qty}:{ctx}", style="success", emoji_id=binance_emoji)],
        [btn(usdt_label, f"pay_usdt_net:{pid}:{qty}:{ctx}", style="success", emoji_id=usdt_emoji)],
        [btn(card_label, f"pay_fiat:{pid}:{qty}:{ctx}", style="primary", emoji_id=card_emoji)],
        [btn(back_label, f"summary:{pid}:{qty}:{ctx}", style="danger", emoji_id=back_emoji)]
    ]
    return "<b>Choose a payment method:</b>", kb(rows)


def render_fiat_payment_methods(uid: int, p: sqlite3.Row, qty: int, ctx: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Card and bank transfer options."""
    _ = uid
    pid = p["id"]
    rate = get_usd_ngn_rate()
    total_ngn = round((p["price"] * qty / 100.0) * rate)
    total_usd = f"{(p['price'] * qty / 100.0):.2f}"

    rows = []
    if is_bachs_configured():
        curr = get_bachs_currency()
        amount_disp = f"₦{total_ngn:,} NGN" if curr == "NGN" else f"${total_usd} USD"
        rows.append([btn(f"💳 Pay with Card / Bank / Transfer (Bachs) — {amount_disp}", f"pay_opt:bachs:{pid}:{qty}:{ctx}", style="success")])
    if is_bank_configured():
        banks = get_bank_accounts()
        if len(banks) == 1:
            bank_label = f"🇳🇬 Direct Bank Transfer ({banks[0]['bank_name']}) — ₦{total_ngn:,}"
        else:
            names = ", ".join(b["bank_name"] for b in banks[:3])
            if len(banks) > 3:
                names += f" +{len(banks)-3} more"
            bank_label = f"🇳🇬 Direct Bank Transfer ({names}) — ₦{total_ngn:,}"
        rows.append([btn(bank_label, f"pay_opt:bank:{pid}:{qty}:{ctx}", style="success")])

    back_emoji = get_custom_emoji_id("back")
    back_label = "Back" if back_emoji else "↩️ Back"
    rows.append([btn(back_label, f"pay_dir:{pid}:{qty}:{ctx}", style="danger", emoji_id=back_emoji)])

    text = (
        "<b>Don't have crypto? Pay with Card or Bank Transfer:</b>\n\n"
        f"💵 <b>Exchange Rate:</b> ₦{rate:,} / USD\n"
        f"➡️ <b>Total Amount:</b> <b>${total_usd} USD</b> (≈ ₦{total_ngn:,} NGN)\n\n"
        "Select your preferred payment method below:"
    )
    return text, kb(rows)



async def start_checkout(event: Message | CallbackQuery, uid: int, p: sqlite3.Row, qty: int, ctx: str):
    """Start checkout summary."""
    if not (1 <= qty <= 1000):
        qty = 1
    pid = p["id"]
    is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
    deliv_type = p["delivery_type"] if ("delivery_type" in p.keys() and p["delivery_type"]) else ("auto" if is_auto else "manual")
    if not is_auto and deliv_type != "manual":
        has_stock_rows = db.execute("SELECT 1 FROM stock WHERE product_id=?", (pid,)).fetchone()
        if has_stock_rows:
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
    parts = c.data.split(":", 3)
    if len(parts) < 4 or not parts[1].isdigit() or not parts[2].isdigit():
        return await c.answer("⚠️ Invalid callback data.", show_alert=True)
    _, pid_str, qty_str, ctx = parts
    pid, qty = int(pid_str), int(qty_str)
    if not (1 <= qty <= 1000):
        return await c.answer("⚠️ Invalid quantity.", show_alert=True)
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)
    text, markup = render_order_summary(uid, p, qty, ctx)
    await edit(c, text, markup)


@dp.callback_query(F.data.startswith("pay_dir:"))
async def on_pay_directly(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    parts = c.data.split(":", 3)
    if len(parts) < 4 or not parts[1].isdigit() or not parts[2].isdigit():
        return await c.answer("⚠️ Invalid callback data.", show_alert=True)
    _, pid_str, qty_str, ctx = parts
    pid, qty = int(pid_str), int(qty_str)
    if not (1 <= qty <= 1000):
        return await c.answer("⚠️ Invalid quantity.", show_alert=True)
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)
    text, markup = render_payment_methods(uid, p, qty, ctx)
    await edit(c, text, markup)


@dp.callback_query(F.data.startswith("pay_usdt_net:"))
async def on_pay_usdt_network(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    parts = c.data.split(":", 3)
    if len(parts) < 4 or not parts[1].isdigit() or not parts[2].isdigit():
        return await c.answer("⚠️ Invalid callback data.", show_alert=True)
    _, pid_str, qty_str, ctx = parts
    pid, qty = int(pid_str), int(qty_str)
    if not (1 <= qty <= 1000):
        return await c.answer("⚠️ Invalid quantity.", show_alert=True)
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return await c.answer(t(c.from_user.id, "out_of_stock"), show_alert=True)

    trc20_emoji = get_custom_emoji_id("trc20") or get_custom_emoji_id("tron")
    bep20_emoji = get_custom_emoji_id("bep20") or get_custom_emoji_id("binance")
    back_emoji = get_custom_emoji_id("back")

    trc20_label = "USDT TRC20" if trc20_emoji else "🔴 USDT TRC20"
    bep20_label = "USDT BEP20" if bep20_emoji else "🟡 USDT BEP20"
    back_label = "Back" if back_emoji else "↩️ Back"

    text = "<b>Choose USDT network:</b>"
    rows = [
        [btn(trc20_label, f"pay_opt:trc20:{pid}:{qty}:{ctx}", style="success", emoji_id=trc20_emoji)],
        [btn(bep20_label, f"pay_opt:bep20:{pid}:{qty}:{ctx}", style="success", emoji_id=bep20_emoji)],
        [btn(back_label, f"pay_dir:{pid}:{qty}:{ctx}", style="danger", emoji_id=back_emoji)]
    ]
    await edit(c, text, kb(rows))


@dp.callback_query(F.data.startswith("pay_fiat:"))
async def on_pay_fiat(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    parts = c.data.split(":", 3)
    if len(parts) < 4 or not parts[1].isdigit() or not parts[2].isdigit():
        return await c.answer("⚠️ Invalid callback data.", show_alert=True)
    _, pid_str, qty_str, ctx = parts
    pid, qty = int(pid_str), int(qty_str)
    if not (1 <= qty <= 1000):
        return await c.answer("⚠️ Invalid quantity.", show_alert=True)
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return await c.answer(t(c.from_user.id, "out_of_stock"), show_alert=True)

    if not is_bank_configured() and not is_bachs_configured():
        return await c.answer("⚠️ Local / fiat payment methods are currently being set up. Please select CryptoPay, Binance Pay, or USDT.", show_alert=True)

    text, markup = render_fiat_payment_methods(c.from_user.id, p, qty, ctx)
    await edit(c, text, markup)


@dp.callback_query(F.data.startswith("pay_opt:"))
async def on_pay_option_selected(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    parts = c.data.split(":", 4)
    if len(parts) < 5 or not parts[2].isdigit() or not parts[3].isdigit():
        return await c.answer("⚠️ Invalid callback data.", show_alert=True)
    _, method, pid_str, qty_str, ctx = parts
    pid, qty = int(pid_str), int(qty_str)
    if not (1 <= qty <= 1000):
        return await c.answer("⚠️ Invalid quantity.", show_alert=True)
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)

    is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
    deliv_type = p["delivery_type"] if ("delivery_type" in p.keys() and p["delivery_type"]) else ("auto" if is_auto else "manual")
    if not is_auto and deliv_type != "manual":
        has_stock_rows = db.execute("SELECT 1 FROM stock WHERE product_id=?", (pid,)).fetchone()
        if has_stock_rows:
            avail = stock_count(pid)
            if avail < qty:
                return await c.answer(f"⚠️ Not enough stock available ({avail} left).", show_alert=True)

    total_cents = p["price"] * qty
    total_usdt = f"{total_cents / 100:.2f}"
    icon = get_product_icon(p["name"], p["category"] if "category" in p.keys() else None)

    # Save pending order
    cur = db.execute(
        "INSERT INTO orders(user_id, product_id, product_name, price, quantity, payment_method, status) VALUES(?,?,?,?,?,?,'pending')",
        (uid, pid, p["name"], total_cents, qty, method)
    )
    oid = cur.lastrowid

    # Bachs payment gateway
    if method == "bachs":
        if not is_bachs_configured():
            db.execute("DELETE FROM orders WHERE id=?", (oid,))
            return await c.answer("⚠️ Bachs payment gateway is not configured yet. Please select another payment method.", show_alert=True)

        currency = get_bachs_currency()
        if currency == "NGN":
            rate = get_usd_ngn_rate()
            amount_disp = f"₦{round((total_cents / 100.0) * rate):,} NGN"
        else:
            amount_disp = f"${total_usdt} USD"

        ok, res = await initialize_bachs_transaction(
            order_id=oid,
            amount_cents=total_cents,
            product_name=p["name"],
            customer_email=None,
            uid=uid,
            qty=qty
        )
        if not ok or not res.get("checkout_url") or not res.get("checkout_id"):
            db.execute("DELETE FROM orders WHERE id=?", (oid,))
            err_msg = res.get("error") or "Unknown error"
            log.error("Bachs checkout creation error for order #%s: %s", oid, err_msg)
            return await c.answer("⚠️ Failed to initialize Bachs checkout. Please try again or select another payment method.", show_alert=True)

        checkout_id = res["checkout_id"]
        checkout_url = res["checkout_url"]
        db.execute("UPDATE orders SET invoice_id=?, txid=?, expected_amount=? WHERE id=?", (checkout_id, checkout_id, amount_disp, oid))

        text = (
            "<u>💳 <b>Online Payment via Bachs</b></u>\n"
            "────────────────────────\n\n"
            f"🥇 <b>Product:</b> {icon} <b>{esc(p['name'])}</b>\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            f"💵 <b>Total Price:</b> <b>{amount_disp}</b>\n"
            "────────────────────────\n"
            "💳 <b>Accepted Payment Methods:</b>\n"
            "• Debit / Credit Cards (Mastercard, Visa, Verve)\n"
            "• Direct Bank Transfer / Virtual Account\n"
            "• Mobile Money & Stablecoins (USDT / USDC)\n\n"
            "👉 <b>Step 1:</b> Tap <b>💳 Pay with Bachs</b> below to open the secure payment page.\n"
            "👉 <b>Step 2:</b> Complete payment on the page.\n"
            "👉 <b>Step 3:</b> Return here and tap <b>✅ I've Paid — Check Status</b>.\n\n"
            "⌛ <i>Your product is delivered instantly upon payment confirmation!</i>"
        )
        rows = [
            [btn("💳 Pay with Bachs", url=checkout_url)],
            [btn("✅ I've Paid — Check Status", f"chk_bachs:{oid}:{checkout_id}", style="success")],
            [btn("↩️ Back", f"pay_fiat:{pid}:{qty}:{ctx}", style="danger")],
            [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
        ]
        return await edit(c, text, kb(rows))

    # Bank transfer
    if method == "bank":
        rate = get_usd_ngn_rate()
        amount_ngn = round((total_cents / 100.0) * rate)
        banks = get_bank_accounts()
        if not banks:
            db.execute("DELETE FROM orders WHERE id=?", (oid,))
            return await c.answer("⚠️ Bank transfer is currently unavailable.", show_alert=True)

        db.execute("UPDATE orders SET expected_amount=?, invoice_id=? WHERE id=?", (f"₦{amount_ngn:,} NGN", f"BANK-{oid}", oid))

        bank_lines = []
        if len(banks) == 1:
            bank = banks[0]
            bank_lines.append(
                f"🏦 <b>Bank Name:</b> <b>{esc(bank['bank_name'])}</b>\n"
                f"🔢 <b>Account Number:</b> <code>{esc(bank['account_number'])}</code>\n"
                "👆 <i>Tap to copy account number.</i>\n"
                f"👤 <b>Account Name:</b> <b>{esc(bank['account_name'])}</b>"
            )
            pay_step_text = "to the account above"
        else:
            for idx, bank in enumerate(banks, 1):
                bank_lines.append(
                    f"🏦 <b>Account #{idx} ({esc(bank['bank_name'])}):</b>\n"
                    f"🔢 <b>Account Number:</b> <code>{esc(bank['account_number'])}</code> <i>(tap to copy)</i>\n"
                    f"👤 <b>Account Name:</b> <b>{esc(bank['account_name'])}</b>"
                )
            pay_step_text = "to ANY of the accounts above"

        banks_display = "\n\n".join(bank_lines)

        text = (
            "<u>🇳🇬 <b>Direct Bank Transfer (Naira)</b></u>\n"
            "────────────────────────\n\n"
            f"🥇 <b>Product:</b> {icon} <b>{esc(p['name'])}</b>\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            f"💵 <b>Price (USD):</b> ${total_usdt} USDT\n"
            f"📈 <b>Rate:</b> ₦{rate:,} / USD\n"
            "➡️ <b>Send exactly this amount:</b>\n"
            f"<code>₦{amount_ngn:,}</code>\n"
            "👆 <i>Tap to copy amount.</i>\n"
            "────────────────────────\n"
            f"{banks_display}\n"
            f"📝 <b>Narration / Remark:</b> <code>CL-{oid}</code>\n"
            "────────────────────────\n\n"
            f"💸 <b>Step 1:</b> Transfer <b>₦{amount_ngn:,}</b> {pay_step_text} via your banking app (OPay, PalmPay, Kuda, GTB, etc.).\n"
            "🔗 <b>Step 2:</b> Tap <b>Submit Transfer Reference / Name</b> below and paste your transaction reference or sender name.\n"
            "✅ <b>Step 3:</b> Our admin verifies the transfer and your order is instantly delivered!\n\n"
            "⌛ <b>Please pay within 60 minutes.</b>"
        )
        rows = [
            [btn("🔗 Submit Transfer Reference / Name", f"submit_tx:{oid}", style="primary")],
            [btn("✅ I've Paid — Check Status", f"chk_manual:{oid}", style="success")],
            [btn("↩️ Back", f"pay_fiat:{pid}:{qty}:{ctx}", style="danger")],
            [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
        ]
        return await edit(c, text, kb(rows))

    # CryptoBot (+3% fee)
    if method == "cryptobot":
        pay_url = None
        inv_id = None
        cb_cents = round(total_cents * 1.03)
        cb_usdt = f"{cb_cents / 100:.2f}"
        if CRYPTOBOT_TOKEN and not CRYPTOBOT_TOKEN.startswith("token-from"):
            try:
                inv = await create_cryptobot_invoice(oid, f"{p['name']} x{qty}", cb_cents)
                inv_id = inv["invoice_id"]
                db.execute("UPDATE orders SET invoice_id=? WHERE id=?", (str(inv_id), oid))
                pay_url = inv.get("bot_invoice_url") or inv.get("pay_url")
            except Exception:
                log.exception("CryptoBot invoice creation failed")

        if not pay_url or not inv_id:
            db.execute("DELETE FROM orders WHERE id=?", (oid,))
            return await c.answer("⚠️ CryptoBot checkout could not be initialized. Please try again or choose another payment method.", show_alert=True)

        user_curr = get_user_currency(c.from_user.id)
        ngn_approx = f" (≈ {format_price(cb_cents, 'NGN')})" if user_curr == "NGN" else ""

        text = (
            "<u>🤖 <b>CryptoPay Checkout (Crypto Bot)</b></u>\n"
            "────────────────────────\n\n"
            f"🥇 <b>Product:</b> {icon} <b>{esc(p['name'])}</b>\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            f"💵 <b>Subtotal:</b> ${total_usdt} USDT\n"
            f"📊 <b>Processing Fee (+3%):</b> ${(cb_cents - total_cents) / 100:.2f} USDT\n"
            f"➡️ <b>Total to pay:</b> <b>${cb_usdt} USDT{ngn_approx}</b>\n"
            "────────────────────────\n\n"
            f"1. Tap <b>Pay ${cb_usdt} USDT</b> below to open CryptoBot.\n"
            "2. Complete payment in Telegram.\n"
            "3. Return and tap <b>I've Paid — Check Status</b> for instant delivery."
        )
        rows = [
            [btn(f"💳 Pay ${cb_usdt} USDT", url=pay_url, style="success")],
            [btn("✅ I've Paid — Check Status", f"chk_cb:{oid}:{inv_id}", style="success")],
            [btn("↩️ Back", f"pay_dir:{pid}:{qty}:{ctx}", style="danger")],
            [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
        ]
        return await edit(c, text, kb(rows))

    # Binance Pay
    if method == "binance":
        user_curr = get_user_currency(c.from_user.id)
        ngn_approx = f" (≈ {format_price(total_cents, 'NGN')})" if user_curr == "NGN" else ""
        text = (
            "<u>🟡 <b>Binance Pay</b></u>\n"
            "────────────────────────\n"
            f"🥇 <b>Product:</b> {icon} <b>{esc(p['name'])}</b>\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            "➡️ <b>Send exactly this amount:</b>\n"
            f"<code>{total_usdt} USDT</code>{ngn_approx}\n"
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
            [btn("↩️ Back", f"pay_dir:{pid}:{qty}:{ctx}", style="danger")],
            [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
        ]
        return await edit(c, text, kb(rows))

    # Add cents to crypto amount so we can identify this order on-chain
    extra_suffix = f"{(oid % 90) + 10:02d}"
    exact_crypto_amount = f"{total_cents / 100:.2f}{extra_suffix}"
    db.execute("UPDATE orders SET expected_amount=? WHERE id=?", (f"{exact_crypto_amount} USDT", oid))
    user_curr = get_user_currency(c.from_user.id)
    ngn_approx = f" (≈ {format_price(total_cents, 'NGN')})" if user_curr == "NGN" else ""

    # USDT on BSC (BEP20)
    if method == "bep20":
        text = (
            "<u>🟠 <b>USDT BEP20 (BEP20)</b></u>\n"
            "────────────────────────\n"
            f"🥇 <b>Product:</b> {icon} <b>{esc(p['name'])}</b>\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            "➡️ <b>Send exactly this amount:</b>\n"
            f"<code>{exact_crypto_amount} USDT</code>{ngn_approx}\n"
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
            [btn("↩️ Back", f"pay_usdt_net:{pid}:{qty}:{ctx}", style="danger")],
            [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
        ]
        return await edit(c, text, kb(rows))

    # USDT on Tron (TRC20)
    if method == "trc20":
        text = (
            "<u>🔴 <b>USDT TRC20 (TRC20)</b></u>\n"
            "────────────────────────\n"
            f"🥇 <b>Product:</b> {icon} <b>{esc(p['name'])}</b>\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            "➡️ <b>Send exactly this amount:</b>\n"
            f"<code>{exact_crypto_amount} USDT</code>{ngn_approx}\n"
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
            [btn("↩️ Back", f"pay_usdt_net:{pid}:{qty}:{ctx}", style="danger")],
            [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
        ]
        return await edit(c, text, kb(rows))


@dp.callback_query(F.data.startswith("submit_tx:"))
async def on_submit_tx(c: CallbackQuery):
    parts = c.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return await c.answer("⚠️ Invalid order data.", show_alert=True)
    oid = int(parts[1])
    uid = c.from_user.id
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await c.answer("Order not found.", show_alert=True)
    if o["user_id"] != uid and uid not in ADMIN_IDS:
        return await c.answer("⚠️ Unauthorized access to this order.", show_alert=True)
    if o["status"] == "completed":
        return await c.answer("✅ This order is already completed.", show_alert=True)
    if o["refunded"] or o["status"] in ("refunded", "cancelled", "rejected"):
        return await c.answer(f"⚠️ This order cannot accept payments (status: {o['status']}).", show_alert=True)
    pending_inputs[uid] = {"action": "txid", "oid": oid}
    method = (o["payment_method"] or "").lower()
    if method == "bank":
        await c.message.answer(
            f"✍️ <b>Submit Bank Transfer Reference for Order #{oid}:</b>\n\n"
            f"Please send your <b>Sender Name</b> or <b>Transfer Transaction Reference / Session ID</b> in this chat.\n"
            f"Our admin will verify the transfer in our bank app and release your order immediately.\n\n"
            f"Send /cancel to return to the order."
        )
    else:
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
    if o["status"] == "completed":
        return True, "Order already completed."
    if o["status"] in ("paid", "manual_pending"):
        return True, "Payment already received and pending fulfillment."
    if o["refunded"] or o["status"] in ("refunded", "cancelled", "rejected"):
        return False, "⚠️ This order was cancelled or refunded."

    # Make sure this txid hasn't already been used
    dup = db.execute(
        "SELECT id FROM orders WHERE LOWER(txid)=LOWER(?) AND status IN ('completed', 'paid', 'manual_pending', 'refunded') AND id != ?",
        (txid.strip(), oid)
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
                f"⚠️ Payment verified on-chain for Order #{oid}, but auto-fulfillment failed: {esc(str(err))}\n"
                f"• Method: {esc(method.upper())}\n"
                f"• TxID: <code>{esc(txid)}</code>\n"
                f"👉 Use <code>/deliver {oid} &lt;item&gt;</code> to deliver manually."
            )
            return True, "Payment verified on-chain! Our support team is completing your delivery shortly."

        if payload == "MANUAL_PENDING":
            await notify_admins(
                bot_instance,
                f"⚡ <b>Payment Auto-Verified On-Chain!</b>\n"
                f"• <b>Order:</b> #{oid}\n"
                f"• <b>Product:</b> {esc(o['product_name'])}\n"
                f"• <b>Amount:</b> ${detected_amount:.2f} USDT (Required: {usd(o['price'])})\n"
                f"• <b>Method:</b> {esc(method.upper())}\n"
                f"• <b>TxID:</b> <code>{esc(txid)}</code>\n"
                f"• <b>Status:</b> Awaiting support fulfillment / Model B top-up"
            )
            return True, "MANUAL_PENDING"

        user_mention = f"User {uid}"
        await notify_admins(
            bot_instance,
            f"⚡ <b>Auto-Verified & Delivered!</b>\n"
            f"• <b>Order:</b> #{oid}\n"
            f"• <b>Buyer:</b> {user_mention}\n"
            f"• <b>Product:</b> {esc(o['product_name'])}\n"
            f"• <b>Amount:</b> ${detected_amount:.2f} USDT (Required: {usd(o['price'])})\n"
            f"• <b>Method:</b> {esc(method.upper())}\n"
            f"• <b>TxID:</b> <code>{esc(txid)}</code>"
        )
        return True, "VERIFIED_AND_DELIVERED"

    return False, details


@dp.callback_query(F.data.startswith("chk_manual:"))
async def on_check_manual_status(c: CallbackQuery):
    parts = c.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return await c.answer("⚠️ Invalid callback data.", show_alert=True)
    oid = int(parts[1])
    uid = c.from_user.id
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await c.answer("Order not found.", show_alert=True)
    if o["user_id"] != uid and uid not in ADMIN_IDS:
        return await c.answer("⚠️ Unauthorized access to this order.", show_alert=True)
    if o["status"] == "completed":
        if o["payload"]:
            await edit(c, t(uid, "purchased", oid=oid, payload=esc(o["payload"])), kb([[btn(t(uid, "btn_back"), "home", style="primary")]]))
        return await c.answer("✅ Order completed and delivered!", show_alert=True)
    if o["status"] == "manual_pending":
        return await c.answer("⏳ Payment received! Your order is currently pending fulfillment by support.", show_alert=True)
    if o["refunded"] or o["status"] in ("refunded", "cancelled", "rejected"):
        return await c.answer("⚠️ This order was cancelled or refunded. Please contact support.", show_alert=True)

    method = (o["payment_method"] or "").lower()
    if not o["txid"]:
        hint = "Submit Transfer Reference / Name" if method == "bank" else "Submit Transaction ID"
        return await c.answer(f"⚠️ Please tap '{hint}' first so we can verify your payment.", show_alert=True)

    if "bep20" in method or "trc20" in method:
        verified, result = await check_and_fulfill_onchain(oid, o["txid"], uid, c.bot)
        if verified and result == "VERIFIED_AND_DELIVERED":
            comp_o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if comp_o and comp_o["payload"] and comp_o["payload"] != "MANUAL_PENDING":
                await edit(c, t(uid, "purchased", oid=oid, payload=esc(comp_o["payload"])), kb([[btn(t(uid, "btn_back"), "home", style="primary")]]))
                return await c.answer("✅ Verified on blockchain and delivered!", show_alert=True)
        elif verified and result == "MANUAL_PENDING":
            return await c.answer("⏳ Payment verified on blockchain! Our support team is fulfilling your order shortly.", show_alert=True)
        elif not verified and result:
            return await c.answer(f"⏳ {result}"[:200], show_alert=True)

    return await c.answer("⏳ Your payment is currently under verification. You will receive your item automatically once confirmed!", show_alert=True)


@dp.callback_query(F.data.startswith("cancel_order:"))
async def on_cancel_order(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    parts = c.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return await c.answer("⚠️ Invalid callback data.", show_alert=True)
    oid = int(parts[1])
    uid = c.from_user.id
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await c.answer("Order not found.", show_alert=True)
    if o["user_id"] != uid and uid not in ADMIN_IDS:
        return await c.answer("⚠️ Unauthorized access to this order.", show_alert=True)
    if o["status"] != "pending":
        return await c.answer(f"⚠️ This order cannot be cancelled (status: {o['status']}).", show_alert=True)
    db.execute("UPDATE orders SET status='cancelled' WHERE id=? AND status='pending'", (oid,))
    await c.answer("❌ Order cancelled.", show_alert=True)
    await edit(c, t(uid, "welcome"), home_kb(uid))


@dp.callback_query(F.data.startswith("buyqty:"))
async def on_buy_qty(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    parts = c.data.split(":", 3)
    if len(parts) < 4 or not parts[1].isdigit() or not parts[2].isdigit():
        return await c.answer("⚠️ Invalid callback data.", show_alert=True)
    _, pid_str, qty_str, ctx = parts
    pid, qty = int(pid_str), int(qty_str)
    if not (1 <= qty <= 1000):
        return await c.answer("⚠️ Invalid quantity.", show_alert=True)
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p or not is_product_available(p):
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)
    await start_checkout(c, uid, p, qty, ctx)


@dp.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    """Quick checkout with quantity 1."""
    pending_inputs.pop(c.from_user.id, None)
    if not c.data.startswith("buy:") or not c.data[4:].isdigit():
        return await c.answer("⚠️ Invalid callback data.", show_alert=True)
    uid, pid = c.from_user.id, int(c.data[4:])
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p or not is_product_available(p):
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)
    await start_checkout(c, uid, p, qty=1, ctx="shop")


@dp.callback_query(F.data.startswith("cqty:"))
async def on_custom_qty(c: CallbackQuery):
    uid = c.from_user.id
    parts = c.data.split(":", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        return await c.answer("⚠️ Invalid callback data.", show_alert=True)
    pid = int(parts[1])
    ctx = parts[2] if len(parts) > 2 else "shop"
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p or not is_product_available(p):
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)

    pending_inputs[uid] = {"action": "cqty", "pid": pid, "ctx": ctx}
    is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
    deliv_type = p["delivery_type"] if ("delivery_type" in p.keys() and p["delivery_type"]) else ("auto" if is_auto else "manual")
    count = stock_count(pid) if (not is_auto and deliv_type != "manual") else 999

    await c.message.answer(
        f"✏️ <b>Enter Custom Quantity for {esc(p['name'])}:</b>\n"
        f"💵 <b>Unit Price:</b> {format_user_price(p['price'], uid)}\n\n"
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
        deliv_type = p["delivery_type"] if ("delivery_type" in p.keys() and p["delivery_type"]) else ("auto" if is_auto else "manual")
        if not is_auto and deliv_type != "manual":
            has_stock_rows = db.execute("SELECT 1 FROM stock WHERE product_id=?", (pid,)).fetchone()
            if has_stock_rows:
                avail = stock_count(pid)
                if qty > avail:
                    pending_inputs[uid] = state
                    return await m.answer(f"⚠️ Only {avail} in stock. Please enter a quantity up to {avail} (or send /cancel):")

        await start_checkout(m, uid, p, qty, ctx)

    elif action == "search":
        raw_term = m.text.strip()
        clean_term = "".join(c for c in raw_term if c.isalnum() or c in (" ", "-", ".")).strip()[:24]
        if not clean_term:
            pending_inputs[uid] = state
            return await m.answer("⚠️ Please enter a valid search keyword (or send /cancel):")
        prods = db.execute(
            "SELECT * FROM products WHERE (LOWER(name) LIKE ? OR LOWER(description) LIKE ?) ORDER BY id",
            (f"%{clean_term.lower()}%", f"%{clean_term.lower()}%")
        ).fetchall()
        text, markup = render_product_list(
            uid, prods, page=1,
            cb_prefix=f"srch:{clean_term}",
            ctx_prefix=f"s_{clean_term}",
            back_cb="shop"
        )
        await m.answer(text, reply_markup=markup)

    elif action == "txid":
        oid = state["oid"]
        txid = m.text.strip()
        if not txid:
            pending_inputs[uid] = state
            return await m.answer("⚠️ Please enter a valid transaction reference or hash (or send /cancel):")
        if len(txid) > 128:
            pending_inputs[uid] = state
            return await m.answer("⚠️ Transaction reference cannot exceed 128 characters (or send /cancel):")

        o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return await m.answer("⚠️ Order not found.")
        if o["user_id"] != uid and uid not in ADMIN_IDS:
            return await m.answer("⚠️ Unauthorized access to this order.")
        if o["status"] == "completed":
            return await m.answer(f"✅ Order #{oid} is already completed.")
        if o["refunded"] or o["status"] in ("refunded", "cancelled", "rejected"):
            return await m.answer(f"⚠️ Order #{oid} was cancelled or refunded. Please contact support.")

        # Make sure this txid hasn't already been used
        dup = db.execute(
            "SELECT id FROM orders WHERE LOWER(txid)=LOWER(?) AND status IN ('completed', 'paid', 'manual_pending', 'refunded') AND id != ?",
            (txid, oid)
        ).fetchone()
        if dup:
            pending_inputs[uid] = state
            return await m.answer(f"⚠️ This Transaction ID was already submitted for Order #{dup['id']}. Please provide a valid, unique transaction reference (or send /cancel):")

        db.execute("UPDATE orders SET txid=? WHERE id=?", (txid, oid))
        method = (o["payment_method"] or "").lower()

        # Try verifying on the blockchain
        if "bep20" in method or "trc20" in method:
            verif_msg = await m.answer("🔍 <i>Verifying transaction on the blockchain...</i>")
            verified, result = await check_and_fulfill_onchain(oid, txid, uid, m.bot)
            try:
                await verif_msg.delete()
            except Exception:
                pass

            if verified:
                if result == "MANUAL_PENDING":
                    return await m.answer(
                        f"🎉 <b>Payment Verified On-Chain for Order #{oid}!</b>\n\n"
                        f"Item: <b>{esc(o['product_name'])}</b>\n\n"
                        f"🟡 <i>Your order has been queued and is being processed by our support team. Your credentials/items will be delivered directly to you here shortly!</i>\n\n"
                        f"If you have any questions, contact Support: {SUPPORT_URL}"
                    )
                elif result == "VERIFIED_AND_DELIVERED":
                    comp_o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
                    if comp_o and comp_o["payload"]:
                        return await m.answer(t(uid, "purchased", oid=oid, payload=esc(comp_o["payload"])))
                    return await m.answer(f"🎉 <b>Payment Verified for Order #{oid}!</b>\n\nItem: <b>{esc(o['product_name'])}</b>\n\nYour order has been confirmed!")
                else:
                    return await m.answer(f"✅ {result}")


        # Forward to admin for review
        if method == "bank":
            await m.answer(
                f"✅ <b>Transfer Reference Received!</b>\n\n"
                f"• <b>Order:</b> #{oid}\n"
                f"• <b>Reference / Name:</b> <code>{esc(txid)}</code>\n\n"
                f"Our admin has been notified and is verifying your bank transfer. Your items will be delivered here as soon as confirmed!"
            )
        else:
            await m.answer(
                f"✅ <b>Transaction ID Received!</b>\n\n"
                f"• <b>Order:</b> #{oid}\n"
                f"• <b>Reference:</b> <code>{esc(txid)}</code>\n\n"
                f"Our team is verifying your payment. Your product will be delivered here automatically once confirmed!"
            )
        user_mention = f"@{m.from_user.username}" if m.from_user.username else f"User {uid}"
        expected_note = f"\n• <b>Expected:</b> {esc(o['expected_amount'])}" if ("expected_amount" in o.keys() and o["expected_amount"]) else ""
        await notify_admins(
            m.bot,
            lambda aid: (
                f"🔔 <b>New Manual Payment Submitted!</b>\n"
                f"• <b>Order:</b> #{oid}\n"
                f"• <b>Buyer:</b> {user_mention} (<code>{uid}</code>)\n"
                f"• <b>Product:</b> {esc(o['product_name'])}\n"
                f"• <b>Amount:</b> {format_user_price(o['price'], aid)}{expected_note}\n"
                f"• <b>Method:</b> {esc(o['payment_method'] or 'Manual')}\n"
                f"• <b>TxID / Sender:</b> <code>{esc(txid)}</code>\n\n"
                f"✅ Approve & deliver: /approve_{oid}\n"
                f"❌ Reject: /reject_{oid}"
            )
        )


@dp.message(F.text.regexp(r"^/approve_(\d+)(?:@\w+)?$"), is_admin)
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
            oid = int(m.text.split("_")[1].split("@")[0])
        except (ValueError, IndexError):
            pass

    if not oid:
        return await m.answer("Usage: /approve <order_id> or /approve_<order_id>")

    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await m.answer(f"Order #{oid} not found.")
    if o["status"] == "completed":
        return await m.answer(f"Order #{oid} is already completed.")
    if o["refunded"] or o["status"] in ("refunded", "cancelled", "rejected"):
        return await m.answer(f"⚠️ Order #{oid} was refunded or cancelled.")

    payload, err = await fulfill_order(m.bot, oid, force_supplier=True)
    if err:
        return await m.answer(f"⚠️ Failed to fulfill order #{oid}: {esc(str(err))}")

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
        await m.answer(f"✅ Order #{oid} fulfilled, but could not message user: {esc(str(e))}\nPayload: <code>{esc(payload)}</code>")


@dp.message(F.text.regexp(r"^/reject_(\d+)(?:@\w+)?$"), is_admin)
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
            oid = int(m.text.split("_")[1].split("@")[0])
        except (ValueError, IndexError):
            pass

    if not oid:
        return await m.answer("Usage: /reject <order_id> or /reject_<order_id>")

    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await m.answer(f"Order #{oid} not found.")
    if o["status"] == "completed":
        return await m.answer(f"⚠️ Order #{oid} is already completed and cannot be rejected.")

    db.execute("UPDATE orders SET status='rejected' WHERE id=?", (oid,))
    user_notified = True
    err_detail = None
    try:
        await m.bot.send_message(o["user_id"], f"❌ Order #{oid} payment verification could not be confirmed. If you believe this is an error, please contact support.")
    except Exception as e:
        user_notified = False
        err_detail = str(e)
    if user_notified:
        await m.answer(f"Order #{oid} marked as rejected.")
    else:
        await m.answer(f"Order #{oid} marked as rejected (⚠️ Could not notify user {o['user_id']}: {esc(err_detail or 'bot blocked or user not reachable')}).")


@dp.callback_query(F.data.startswith("chk_cb:"))
async def check_cryptobot_payment(c: CallbackQuery):
    parts = c.data.split(":")
    if len(parts) < 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return await c.answer("⚠️ Invalid payment callback data.", show_alert=True)
    oid, inv_id = int(parts[1]), int(parts[2])
    uid = c.from_user.id

    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await c.answer("Order not found.", show_alert=True)
    if o["user_id"] != uid and uid not in ADMIN_IDS:
        return await c.answer("⚠️ Unauthorized access to this order.", show_alert=True)
    if o["status"] == "completed":
        if o["payload"]:
            await c.message.answer(t(uid, "purchased", oid=oid, payload=esc(o["payload"])))
        return await c.answer("✅ Order already completed!", show_alert=False)
    if o["status"] == "manual_pending":
        return await c.answer("⏳ Payment confirmed! Your order is currently pending fulfillment by support.", show_alert=True)
    if o["refunded"] or o["status"] in ("refunded", "cancelled", "rejected"):
        return await c.answer("⚠️ This order was cancelled or refunded. Please contact support.", show_alert=True)

    if not o["invoice_id"] or str(o["invoice_id"]) != str(inv_id):
        return await c.answer("⚠️ Invoice ID does not match this order.", show_alert=True)

    dup = db.execute(
        "SELECT id FROM orders WHERE invoice_id=? AND status IN ('completed', 'paid', 'manual_pending', 'refunded') AND id != ?",
        (str(inv_id), oid)
    ).fetchone()
    if dup:
        return await c.answer(f"⚠️ This invoice was already claimed for Order #{dup['id']}.", show_alert=True)

    paid = await check_cryptobot_invoice(inv_id)
    if paid:
        db.execute("UPDATE orders SET invoice_id=?, status='paid' WHERE id=?", (str(inv_id), oid))
        payload, err = await fulfill_order(c.bot, oid)
        if payload == "MANUAL_PENDING":
            await c.message.answer(
                f"🎉 <b>Payment Confirmed for Order #{oid}!</b>\n\n"
                f"Item: <b>{esc(o['product_name'])}</b>\n\n"
                f"🟡 <i>Your order has been queued and is being processed by our support team. Your credentials/items will be delivered directly to you here shortly!</i>\n\n"
                f"If you have any questions, contact Support: {SUPPORT_URL}"
            )
            await c.answer()
        elif payload:
            await c.message.answer(t(uid, "purchased", oid=oid, payload=esc(payload)))
            await notify_admins(c.bot, lambda aid: f"✅ Order #{oid} paid via @CryptoBot & delivered: {esc(o['product_name'])} ({format_user_price(o['price'], aid)}) to user {uid}")
            await c.answer()
        else:
            await c.answer(t(uid, err or "supplier_error")[:200], show_alert=True)
            await notify_admins(c.bot, f"⚠️ Order #{oid} paid via @CryptoBot by user {uid} but delivery failed: {esc(str(err))}")
    else:
        await c.answer(t(uid, "pay_pending"), show_alert=True)


@dp.callback_query(F.data.startswith("chk_bachs:"))
async def check_bachs_payment_callback(c: CallbackQuery):
    parts = c.data.split(":", 2)
    if len(parts) < 3 or not parts[1].isdigit():
        return await c.answer("⚠️ Invalid payment callback data.", show_alert=True)
    oid = int(parts[1])
    checkout_id = parts[2].strip()
    uid = c.from_user.id

    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await c.answer("Order not found.", show_alert=True)
    if o["user_id"] != uid and uid not in ADMIN_IDS:
        return await c.answer("⚠️ Unauthorized access to this order.", show_alert=True)
    if o["status"] == "completed":
        if o["payload"]:
            await c.message.answer(t(uid, "purchased", oid=oid, payload=esc(o["payload"])))
        return await c.answer("✅ Order already completed!", show_alert=False)
    if o["status"] == "manual_pending":
        return await c.answer("⏳ Payment confirmed! Your order is currently pending fulfillment by support.", show_alert=True)
    if o["refunded"] or o["status"] in ("refunded", "cancelled", "rejected"):
        return await c.answer("⚠️ This order was cancelled or refunded. Please contact support.", show_alert=True)

    if not o["invoice_id"] or str(o["invoice_id"]) != str(checkout_id):
        return await c.answer("⚠️ Invoice ID does not match this order.", show_alert=True)

    # Make sure this payment hasn't already been claimed
    dup = db.execute(
        "SELECT id FROM orders WHERE (txid=? OR invoice_id=?) AND status IN ('completed', 'paid', 'manual_pending', 'refunded') AND id != ?",
        (checkout_id, checkout_id, oid)
    ).fetchone()
    if dup:
        return await c.answer(f"⚠️ This payment was already claimed for Order #{dup['id']}.", show_alert=True)

    is_paid, res = await verify_bachs_transaction(checkout_id)
    if is_paid:
        curr = (res.get("currency") or get_bachs_currency() or "USD").strip().upper()
        paid_amount_str = res.get("amount")
        if paid_amount_str is not None:
            try:
                paid_amount = float(paid_amount_str)
                if paid_amount <= 0:
                    return await c.answer("⚠️ Amount paid does not match order total. Please contact support.", show_alert=True)

                if curr == "NGN":
                    rate = get_usd_ngn_rate()
                    expected_amount = (o["price"] / 100.0) * rate
                    curr_symbol = "₦"
                else:
                    expected_amount = o["price"] / 100.0
                    curr_symbol = "$"

                # 2% tolerance for exchange rate changes
                if paid_amount < (expected_amount * 0.98):
                    log.warning(
                        "Bachs payment underpaid for order #%s: received %s %s, expected %s %s",
                        oid, paid_amount, curr, expected_amount, curr
                    )
                    return await c.answer(
                        f"⚠️ Amount paid ({curr_symbol}{paid_amount:,.2f}) does not match expected total ({curr_symbol}{expected_amount:,.2f}). Please contact support.",
                        show_alert=True
                    )
            except (ValueError, TypeError):
                pass

        db.execute(
            "UPDATE orders SET txid=?, invoice_id=?, payment_method='bachs', status='paid' WHERE id=?",
            (checkout_id, checkout_id, oid)
        )
        payload, err = await fulfill_order(c.bot, oid)
        if payload == "MANUAL_PENDING":
            await c.message.answer(
                f"🎉 <b>Payment Confirmed for Order #{oid}!</b>\n\n"
                f"Item: <b>{esc(o['product_name'])}</b>\n\n"
                f"🟡 <i>Your order has been queued and is being processed by our support team. Your credentials/items will be delivered directly to you here shortly!</i>\n\n"
                f"If you have any questions, contact Support: {SUPPORT_URL}"
            )
            await c.answer()
        elif payload:
            await c.message.answer(t(uid, "purchased", oid=oid, payload=esc(payload)))
            await notify_admins(
                c.bot,
                lambda aid: f"✅ Order #{oid} paid via Bachs & delivered: {esc(o['product_name'])} ({format_user_price(o['price'], aid)}) to user {uid} (Bachs ID: <code>{esc(checkout_id)}</code>)"
            )
            await c.answer()
        else:
            await c.answer(t(uid, err or "supplier_error")[:200], show_alert=True)
            await notify_admins(
                c.bot,
                f"⚠️ Order #{oid} paid via Bachs by user {uid} but delivery failed: {esc(str(err))}\nBachs ID: <code>{esc(checkout_id)}</code>"
            )
    else:
        await c.answer("⏳ Payment is still pending on Bachs. Please complete payment on the checkout page, then tap again.", show_alert=True)


@dp.callback_query(F.data.startswith("manual_pay:"))
async def manual_pay_info(c: CallbackQuery):
    parts = c.data.split(":")
    if len(parts) < 2 or not parts[1].isdigit():
        return await c.answer("⚠️ Invalid callback data.", show_alert=True)
    oid = int(parts[1])
    uid = c.from_user.id
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await c.answer("Order not found.", show_alert=True)
    if o["user_id"] != uid and uid not in ADMIN_IDS:
        return await c.answer("⚠️ Unauthorized access to this order.", show_alert=True)
    details = manual_crypto_details()
    amount_str = format_user_price(o["price"], uid)
    text = t(uid, "manual_crypto_info", oid=oid, name=esc(o["product_name"]), amount=amount_str, details=details)
    await edit(c, text, kb([[btn(t(uid, "btn_support"), url=SUPPORT_URL, style="primary")],
                            [btn(t(uid, "btn_back"), f"p:{o['product_id']}", style="danger")] ]))


@dp.callback_query(F.data == "orders")
@dp.message(Command("order"))
@dp.message(Command("orders"))
@dp.message(Command("purchases"))
async def recent_purchases(event: Message | CallbackQuery):
    """Show user order history."""
    uid = event.from_user.id
    bot_obj = event.bot
    passed, missing = await check_user_fsub(bot_obj, uid)
    if not passed:
        text, markup = render_fsub_gate(missing)
        if isinstance(event, CallbackQuery):
            return await edit(event, text, markup)
        else:
            return await event.answer(text, reply_markup=markup)

    rows = db.execute(
        "SELECT * FROM orders WHERE user_id=? AND status IN ('completed', 'manual_pending', 'paid', 'failed') ORDER BY id DESC LIMIT 10",
        (uid,)
    ).fetchall()

    if not rows:
        text = t(uid, "no_orders")
    else:
        def render_order_item(o):
            p_disp = format_user_price(o['price'], uid)
            if o["status"] == "completed" and o["payload"]:
                return f"📦 <b>Order #{o['id']}</b> · {esc(o['product_name'])} · {p_disp}\n<code>{esc(o['payload'])}</code>"
            elif o["status"] == "failed":
                return f"🔴 <b>Order #{o['id']}</b> · {esc(o['product_name'])} · {p_disp}\n<i>Status: ⚠️ Delivery issue — Please contact support</i>"
            else:
                return f"🟡 <b>Order #{o['id']}</b> · {esc(o['product_name'])} · {p_disp}\n<i>Status: Processing / Awaiting support delivery</i>"

        text = t(uid, "orders_title") + "\n\n" + "\n\n".join(render_order_item(o) for o in rows)
        while len(text) > 3900 and len(rows) > 1:
            rows = rows[:-1]
            text = t(uid, "orders_title") + "\n\n" + "\n\n".join(render_order_item(o) for o in rows)

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
    current_lang = get_user(uid)["lang"]
    rows = []
    for code, name in LANG_NAMES.items():
        mark = " ✅" if code == current_lang else ""
        rows.append([btn(f"{name}{mark}", f"setlang:{code}", style="primary")])
    rows.append([btn(t(uid, "btn_back"), "home", style="danger")])
    await edit(c, t(uid, "lang_choose"), kb(rows))


@dp.callback_query(F.data.startswith("setlang:"))
async def set_lang(c: CallbackQuery):
    parts = c.data.split(":", 1)
    if len(parts) < 2:
        return await c.answer("⚠️ Invalid language callback data.", show_alert=True)
    code = parts[1]
    if code in TR:
        get_user(c.from_user.id)
        db.execute("UPDATE users SET lang=? WHERE id=?", (code, c.from_user.id))
        await c.answer(t(c.from_user.id, "lang_set"), show_alert=False)
    uid = c.from_user.id
    await edit(c, t(uid, "welcome"), home_kb(uid))


@dp.message(Command("lang"))
@dp.message(Command("language"))
async def cmd_lang(m: Message):
    uid = m.from_user.id
    get_user(uid, m.from_user.language_code)
    current_lang = get_user(uid)["lang"]
    rows = []
    for code, name in LANG_NAMES.items():
        mark = " ✅" if code == current_lang else ""
        rows.append([btn(f"{name}{mark}", f"setlang:{code}", style="primary")])
    rows.append([btn(t(uid, "btn_back"), "home", style="danger")])
    await m.answer(t(uid, "lang_choose"), reply_markup=kb(rows))


@dp.callback_query(F.data == "currency")
async def currency_menu(c: CallbackQuery):
    uid = c.from_user.id
    current_curr = get_user_currency(uid)
    currency_emoji_id = get_custom_emoji_id("currency")
    rows = []
    for code, name in CURRENCY_NAMES.items():
        mark = " ✅" if code == current_curr else ""
        rows.append([btn(f"{name}{mark}", f"setcurrency:{code}", style="primary", emoji_id=currency_emoji_id)])
    rows.append([btn(t(uid, "btn_back"), "home", style="danger")])
    await edit(c, t(uid, "currency_choose"), kb(rows))


@dp.callback_query(F.data.startswith("setcurrency:"))
async def set_currency(c: CallbackQuery):
    parts = c.data.split(":", 1)
    if len(parts) < 2:
        return await c.answer("⚠️ Invalid currency callback data.", show_alert=True)
    code = parts[1].upper()
    uid = c.from_user.id
    if code in CURRENCY_NAMES:
        get_user(uid)
        db.execute("UPDATE users SET currency=? WHERE id=?", (code, uid))
        await c.answer(t(uid, "currency_set", currency=code), show_alert=False)
    await edit(c, t(uid, "welcome"), home_kb(uid))


@dp.message(Command("currency"))
async def cmd_currency(m: Message):
    uid = m.from_user.id
    get_user(uid, m.from_user.language_code)
    current_curr = get_user_currency(uid)
    currency_emoji_id = get_custom_emoji_id("currency")
    rows = []
    for code, name in CURRENCY_NAMES.items():
        mark = " ✅" if code == current_curr else ""
        rows.append([btn(f"{name}{mark}", f"setcurrency:{code}", style="primary", emoji_id=currency_emoji_id)])
    rows.append([btn(t(uid, "btn_back"), "home", style="danger")])
    await m.answer(t(uid, "currency_choose"), reply_markup=kb(rows))


# Admin commands
@dp.message(Command("admin"), is_admin)
async def admin_help(m: Message):
    await m.answer(
        "<b>Admin Commands</b>\n\n"
        "<b>Catalog & Products</b>\n"
        "/products - List all products with margins\n"
        "/addproduct &lt;price&gt; [currency] &lt;name&gt; [| description] (e.g. $12.50 or 15000 NGN)\n"
        "/setprice &lt;product_id&gt; &lt;price&gt; [currency] (e.g. /setprice 1 15000 NGN or $12.50)\n"
        "/setmargin &lt;product_id&gt; &lt;margin_%&gt; (e.g. /setmargin 1 60%)\n"
        "/toggle &lt;product_id&gt;  (activate/deactivate)\n"
        "/addstock &lt;product_id&gt; &lt;item&gt;  (manual items, 1 per line)\n"
        "/refund &lt;order_id&gt;\n"
        "/stats - View sales stats\n"
        "/broadcast &lt;text&gt; - Broadcast message\n"
        "/backup - Download fresh database backup (.db)\n"
        "/export - Download updated products Excel (.xlsx)\n"
        "/setrate [rate_ngn] - View or update USD to NGN exchange rate\n"
        "/currency - Switch your active display currency (USD / NGN)\n"
        "/setbachs [api_key] - Configure Bachs Payments Gateway\n"
        "/bachs - View Bachs Gateway configuration & status\n"
        "/bachscurrency [USD|NGN] - Set Bachs checkout currency\n"
        "/setbank [Bank | AccNum | Name] - Configure or update Direct Bank Transfer\n"
        "/addbank &lt;Bank | AccNum | Name&gt; - Add another bank account (multi-bank)\n"
        "/delbank [number|all] - Remove a bank account or disable bank transfer\n"
        "/bank - View current Bank Transfer configuration\n\n"
        "🎨 <b>Brand Custom Emojis</b>\n"
        "/setemoji &lt;key&gt; [emoji] - Set official brand custom emoji ID\n"
        "/emojis - View configured brand custom emojis\n"
        "/delemoji &lt;key&gt; - Remove custom emoji\n\n"
        "🌐 <b>Supplier Catalog (Automated)</b>\n"
        "/supplier - Supplier balance & account info\n"
        "/mode [auto|manual|instant] - View or switch Model A / Model B\n"
        "/s_products - List available supplier products\n"
        "/s_sync [markup_%] - Auto-sync catalog with profit margin\n"
        "/s_link &lt;local_id&gt; &lt;supplier_id&gt; - Link item to supplier\n"
        "/s_unlink &lt;local_id&gt; - Revert to manual stock\n"
        "/s_orders - Recent supplier orders\n\n"
        "⚡ <b>Fulfillment & Approval</b>\n"
        "/approve &lt;order_id&gt; - Approve & buy from supplier after top-up\n"
        "/deliver &lt;order_id&gt; &lt;item&gt; - Deliver manually to buyer\n\n"
        "🔒 <b>Force-Subscription (FSUB)</b>\n"
        "/fsub - View status & manage mandatory join channels"
    )


@dp.message(Command("fsub"), is_admin)
async def admin_fsub(m: Message, command: CommandObject = None):
    args = (command.args or "").strip() if command else ""
    if not args:
        enabled = is_fsub_enabled()
        channels = get_fsub_channels()
        ch_list = "\n".join(f"  {i+1}. <code>{esc(c)}</code>" for i, c in enumerate(channels)) if channels else "  <i>None configured</i>"
        status_badge = "🟢 <b>Enabled</b>" if enabled else "🔴 <b>Disabled</b>"
        return await m.answer(
            f"🔒 <b>Force-Subscribe (FSUB) Management</b>\n"
            f"────────────────────────\n"
            f"• <b>Status:</b> {status_badge}\n"
            f"• <b>Required Channels/Groups:</b>\n{ch_list}\n"
            f"────────────────────────\n"
            f"<b>Commands:</b>\n"
            f"• <code>/fsub on</code> — Enable gate\n"
            f"• <code>/fsub off</code> — Disable gate\n"
            f"• <code>/fsub add &lt;@channel_or_group&gt;</code> — Add channel\n"
            f"• <code>/fsub remove &lt;@channel_or_group&gt;</code> — Remove channel\n"
            f"• <code>/fsub set &lt;@c1,@c2&gt;</code> — Replace all channels\n\n"
            f"💡 <i>Tip: The bot must be an Administrator in the required channels/groups to check membership!</i>"
        )

    parts = args.split(maxsplit=1)
    subcmd = parts[0].lower()
    subarg = parts[1].strip() if len(parts) > 1 else ""

    if subcmd == "on":
        set_fsub_enabled(True)
        return await m.answer("✅ Force-Subscription is now <b>ENABLED</b>.")
    elif subcmd == "off":
        set_fsub_enabled(False)
        return await m.answer("🔴 Force-Subscription is now <b>DISABLED</b>.")
    elif subcmd == "add" and subarg:
        channels = get_fsub_channels()
        if subarg not in channels:
            channels.append(subarg)
            set_fsub_channels(channels)
            set_fsub_enabled(True)
            return await m.answer(f"✅ Added <code>{esc(subarg)}</code> to required channels. FSUB is active.")
        return await m.answer(f"ℹ️ <code>{esc(subarg)}</code> is already in the list.")
    elif subcmd == "remove" and subarg:
        channels = get_fsub_channels()
        target = subarg.lower()
        new_channels = [c for c in channels if c.lower() != target and c.lstrip("@").lower() != target.lstrip("@")]
        set_fsub_channels(new_channels)
        return await m.answer(f"✅ Removed <code>{esc(subarg)}</code>. Remaining: {len(new_channels)} channels.")
    elif subcmd == "set" and subarg:
        new_channels = [c.strip() for c in subarg.split(",") if c.strip()]
        set_fsub_channels(new_channels)
        set_fsub_enabled(True)
        return await m.answer(f"✅ Configured {len(new_channels)} required channels: {', '.join(new_channels)}")
    elif subcmd in ("preview", "test"):
        channels = get_fsub_channels()
        if not channels:
            return await m.answer("⚠️ No channels configured yet. Use <code>/fsub add @channel</code> first.")
        text, markup = render_fsub_gate(channels)
        return await m.answer(text, reply_markup=markup)
    elif subcmd in ("testmode", "checkadmin"):
        current = get_setting("fsub_check_admin", "0")
        new_val = "0" if current == "1" else "1"
        set_setting("fsub_check_admin", new_val)
        status = "ENABLED (Admins will now see the join gate on /start)" if new_val == "1" else "DISABLED (Admins bypass the gate)"
        return await m.answer(f"⚙️ Admin Gate Check is now <b>{status}</b>.")
    else:
        return await m.answer("Usage: /fsub [on | off | preview | checkadmin | add <@channel> | remove <@channel> | set <@c1,@c2>]")


@dp.message(Command("setemoji"), is_admin)
async def admin_setemoji(m: Message, command: CommandObject = None):
    """
    Sets custom emoji ID for a brand, category, or UI button.
    Usage: /setemoji <key> [custom_emoji_or_id]
    Examples:
      /setemoji chatgpt  (with custom emoji sent in the message)
      /setemoji chatgpt 5368324170671202288
      /setemoji gemini
    """
    args = (command.args or "").strip() if command else ""
    if not args:
        avail_keys = ", ".join(k for k, _, _ in CATEGORIES)
        return await m.answer(
            f"<b>Usage:</b> <code>/setemoji &lt;key&gt; [emoji]</code>\n\n"
            f"Send with a custom emoji or paste its numeric ID.\n\n"
            f"<b>Payment & Navigation Keys:</b>\n"
            f"<code>trc20, bep20, binance, cryptopay, usdt, card, back, bank</code>\n\n"
            f"<b>Category Keys:</b>\n<code>{avail_keys}, shop, orders, lang, support, news, search, all_items</code>"
        )

    parts = args.split(None, 1)
    key = parts[0].strip().lower()

    # Check for custom emoji in message
    emoji_id = None
    if m.entities:
        for ent in m.entities:
            if ent.type == "custom_emoji" and ent.custom_emoji_id:
                emoji_id = str(ent.custom_emoji_id)
                break

    # Check if numeric ID was provided
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

    db.execute("""
        CREATE TABLE IF NOT EXISTS custom_emojis(
          key TEXT PRIMARY KEY,
          emoji_id TEXT NOT NULL,
          created TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    db.execute(
        "INSERT INTO custom_emojis(key, emoji_id) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET emoji_id=excluded.emoji_id",
        (key, emoji_id)
    )
    await m.answer(
        f"✅ Brand emoji for <b>{esc(key)}</b> updated!\n"
        f"• Custom Emoji ID: <code>{esc(emoji_id)}</code>"
    )


@dp.message(Command("getemoji"), is_admin)
async def admin_get_emoji(m: Message):
    """Extract custom emoji ID from a message."""
    found = []
    if m.entities:
        for ent in m.entities:
            if ent.type == "custom_emoji" and ent.custom_emoji_id:
                found.append(str(ent.custom_emoji_id))
    if not found and m.reply_to_message and m.reply_to_message.entities:
        for ent in m.reply_to_message.entities:
            if ent.type == "custom_emoji" and ent.custom_emoji_id:
                found.append(str(ent.custom_emoji_id))

    if not found:
        return await m.answer(
            "<b>How to find a Custom Emoji ID:</b>\n\n"
            "1. Send <code>/getemoji &lt;custom emoji&gt;</code> with a custom emoji from your Telegram sticker/emoji pack.\n"
            "2. Or reply to any message containing custom emojis with <code>/getemoji</code>.\n"
            "3. You can also send the emoji to the public bot <b>@EmojiID</b> or <b>@get_emoji_id_bot</b> to get its ID."
        )

    lines = ["<b>Detected Custom Emoji ID(s):</b>\n"]
    for eid in found:
        lines.append(f"• ID: <code>{esc(eid)}</code>")
    lines.append("\n<b>Quick Setup Commands:</b>")
    first = found[0]
    lines.append(f"• Set TRC20: <code>/setemoji trc20 {first}</code>")
    lines.append(f"• Set BEP20: <code>/setemoji bep20 {first}</code>")
    lines.append(f"• Set Back: <code>/setemoji back {first}</code>")
    await m.answer("\n".join(lines))


@dp.message(Command("emojis"), is_admin)
async def admin_list_emojis(m: Message):
    """List configured brand emojis."""
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
async def admin_del_emoji(m: Message, command: CommandObject = None):
    args = (command.args or "").strip() if command else ""
    if not args:
        return await m.answer("Usage: /delemoji <key>")
    key = args.lower()
    db.execute("DELETE FROM custom_emojis WHERE key=?", (key,))
    await m.answer(f"Custom emoji for <b>{esc(key)}</b> removed.")


def chunk_lines(lines: List[str], max_chars: int = 3500) -> List[str]:
    """Split long messages into smaller chunks."""
    chunks = []
    current_chunk = []
    current_len = 0
    for line in lines:
        line_len = len(line) + 1
        if current_len + line_len > max_chars and current_chunk:
            chunks.append("\n".join(current_chunk))
            current_chunk = [line]
            current_len = line_len
        else:
            current_chunk.append(line)
            current_len += line_len
    if current_chunk:
        chunks.append("\n".join(current_chunk))
    return chunks


@dp.message(Command("products"), is_admin)
async def admin_products(m: Message):
    rows = db.execute("SELECT * FROM products ORDER BY id").fetchall()
    if not rows:
        return await m.answer("No products.")
    admin_curr = get_user_currency(m.from_user.id)
    lines = [f"📦 <b>Store Products (Currency: {admin_curr}):</b>\n"]
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
        is_fixed = bool(p["fixed_price"]) if ("fixed_price" in p.keys() and p["fixed_price"]) else False
        if is_fixed and p["margin"] is not None:
            margin_info = f" · fixed (margin ~{p['margin']}%)"
        elif is_fixed:
            margin_info = " · fixed"
        elif p["margin"] is not None:
            margin_info = f" · margin +{p['margin']}%"
        elif p["supplier_price"] and p["supplier_price"] > 0:
            cost = p["supplier_price"]
            calc_m = round(((p["price"] - cost) / cost) * 100)
            margin_info = f" · margin ~{calc_m}%"

        price_disp = format_price(p["price"], admin_curr)
        cost_disp = f" (cost {format_price(p['supplier_price'], admin_curr)})" if p["supplier_price"] else ""

        cat_disp = f" [{p['category']}]" if ("category" in p.keys() and p["category"]) else ""
        lines.append(
            f"{status_badge} #{p['id']}. <b>{esc(p['name'])}</b>{cat_disp} · {price_disp}{cost_disp}{margin_info}"
        )

    for chunk in chunk_lines(lines):
        await m.answer(chunk)


@dp.message(Command("addproduct"), is_admin)
async def admin_addproduct(m: Message, command: CommandObject = None):
    try:
        if not command or not command.args:
            return await m.answer(
                "Usage: /addproduct <price> [currency] <name> [| description]\n"
                "Examples:\n"
                "• <code>/addproduct $12.50 Netflix 1 Month</code>\n"
                "• <code>/addproduct 15000 NGN ChatGPT Plus</code>\n"
                "• <code>/addproduct ₦15000 Gemini Advanced</code>\n"
                "• <code>/addproduct 12.50 Canva Pro</code> (uses your active currency)"
            )
        parts = command.args.split()
        if not parts:
            raise ValueError("No arguments")
        admin_curr = get_user_currency(m.from_user.id)

        # Check if currency code comes before or after price
        if len(parts) >= 2 and parts[0].upper() in ("USD", "NGN", "NAIRA", "$", "₦"):
            price_raw = f"{parts[1]} {parts[0]}"
            rest = command.args.split(None, 2)[2] if len(parts) > 2 else ""
        elif len(parts) >= 2 and parts[1].upper() in ("USD", "NGN", "NAIRA", "$", "₦"):
            price_raw = f"{parts[0]} {parts[1]}"
            rest = command.args.split(None, 2)[2] if len(parts) > 2 else ""
        else:
            price_raw = parts[0]
            rest = command.args.split(None, 1)[1] if len(parts) > 1 else ""

        if not rest.strip():
            return await m.answer("⚠️ Please provide a product name.")

        try:
            price_cents, set_curr, orig_val = parse_currency_amount(price_raw, default_curr=admin_curr)
        except ValueError as ve:
            if "greater than" in str(ve).lower():
                return await m.answer("⚠️ Price must be greater than $0.00.")
            raise

        name, _, desc = rest.partition("|")
        cat = detect_category(name.strip())
        pid = db.execute("INSERT INTO products(name, description, price, category, fixed_price) VALUES(?,?,?,?,1)",
                         (name.strip(), desc.strip(), price_cents, cat)).lastrowid
        disp_price = f"<b>{format_price(price_cents, admin_curr)}</b>"
        await m.answer(f"Product #{pid} added ({cat}) · Price: {disp_price}.")
    except Exception:
        await m.answer(
            "Usage: /addproduct <price> [currency] <name> [| description]\n"
            "Examples:\n"
            "• <code>/addproduct $12.50 Netflix 1 Month</code>\n"
            "• <code>/addproduct 15000 NGN ChatGPT Plus</code>\n"
            "• <code>/addproduct ₦15000 Gemini Advanced</code>\n"
            "• <code>/addproduct 12.50 Canva Pro</code> (uses your active currency)"
        )


@dp.message(Command("setprice"), is_admin)
async def admin_setprice(m: Message, command: CommandObject = None):
    try:
        if not command or not command.args:
            return await m.answer(
                "Usage: /setprice <product_id> <price> [currency]\n"
                "Examples:\n"
                "• <code>/setprice 1 12.50</code> (uses your active currency)\n"
                "• <code>/setprice 1 $12.50</code> or <code>/setprice 1 12.50 USD</code>\n"
                "• <code>/setprice 1 ₦15000</code> or <code>/setprice 1 15000 NGN</code>"
            )
        parts = command.args.split()
        if len(parts) < 2:
            return await m.answer(
                "Usage: /setprice <product_id> <price> [currency]\n"
                "Examples: /setprice 1 12.50, /setprice 1 15000 NGN, /setprice 1 ₦15000"
            )
        pid = int(parts[0])
        price_raw = " ".join(parts[1:])

        admin_curr = get_user_currency(m.from_user.id)
        try:
            new_price_cents, set_curr, orig_val = parse_currency_amount(price_raw, default_curr=admin_curr)
        except ValueError as ve:
            if "greater than" in str(ve).lower():
                return await m.answer("⚠️ Price must be greater than $0.00.")
            raise

        p = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            return await m.answer(f"Product #{pid} not found.")

        disp_price = f"<b>{format_price(new_price_cents, admin_curr)}</b>"
        # Lock in fixed price and calculate margin if supplier cost is known
        if p["supplier_price"] and p["supplier_price"] > 0:
            cost = p["supplier_price"]
            calc_margin = round(((new_price_cents - cost) / cost) * 100)
            db.execute("UPDATE products SET price=?, margin=?, fixed_price=1 WHERE id=?", (new_price_cents, calc_margin, pid))
            await m.answer(
                f"✅ Price for #{pid} (<b>{esc(p['name'])}</b>) updated to {disp_price}.\n"
                f"• Wholesale Cost: <b>{format_price(cost, admin_curr)}</b>\n"
                f"• Adjusted Margin: <b>{calc_margin}%</b> (fixed price locked: future supplier cost changes will adjust the margin, keeping this selling price unchanged)"
            )
        else:
            db.execute("UPDATE products SET price=?, fixed_price=1 WHERE id=?", (new_price_cents, pid))
            await m.answer(f"✅ Price for #{pid} (<b>{esc(p['name'])}</b>) updated to {disp_price}.")
    except Exception:
        await m.answer(
            "Usage: /setprice <product_id> <price> [currency]\n"
            "Examples:\n"
            "• <code>/setprice 1 12.50</code>\n"
            "• <code>/setprice 1 $12.50</code> or <code>/setprice 1 12.50 USD</code>\n"
            "• <code>/setprice 1 ₦15000</code> or <code>/setprice 1 15000 NGN</code>"
        )



@dp.message(Command("setmargin"), is_admin)
async def admin_setmargin(m: Message, command: CommandObject = None):
    try:
        if not command or not command.args:
            return await m.answer("Usage: /setmargin <product_id> <margin_percent>\nExamples: /setmargin 1 50% or /setmargin 1 35")
        pid, raw_margin = command.args.split()
        pid = int(pid)
        margin = parse_profit_margin(raw_margin)
        p = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            return await m.answer(f"Product #{pid} not found.")

        cost = p["supplier_price"] or p["price"]
        new_price = max(100, round(cost * (1 + margin / 100)))
        db.execute("UPDATE products SET margin=?, price=?, fixed_price=0 WHERE id=?", (margin, new_price, pid))

        admin_curr = get_user_currency(m.from_user.id)
        cost_info = f"• Wholesale Cost: <b>{format_price(cost, admin_curr)}</b>\n" if p["supplier_price"] else ""
        disp_price = f"<b>{format_price(new_price, admin_curr)}</b>"
        await m.answer(
            f"✅ Profit margin for #{pid} (<b>{esc(p['name'])}</b>) set to <b>{margin}%</b>.\n"
            f"{cost_info}"
            f"• New Selling Price: {disp_price}"
        )
    except Exception:
        await m.answer("Usage: /setmargin <product_id> <margin_percent>\nExamples: /setmargin 1 50% or /setmargin 1 35")


@dp.message(Command("toggle"), is_admin)
async def admin_toggle(m: Message, command: CommandObject = None):
    try:
        if not command or not command.args:
            return await m.answer("Usage: /toggle <product_id>")
        pid = int(command.args.strip())
        p = db.execute("SELECT id, name, active FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            return await m.answer(f"Product #{pid} not found.")
        new_active = 1 - p["active"]
        db.execute("UPDATE products SET active=? WHERE id=?", (new_active, pid))
        status_str = "🟢 Active (Enabled)" if new_active else "🔴 Inactive (Disabled)"
        await m.answer(f"Product #{pid} (<b>{esc(p['name'])}</b>) is now {status_str}.")
    except Exception:
        await m.answer("Usage: /toggle <product_id>")


@dp.message(Command("addstock"), is_admin)
async def admin_addstock(m: Message, command: CommandObject = None):
    try:
        if not command or not command.args:
            return await m.answer("Usage: /addstock <product_id> <item>  (one item per line)")
        pid, items = command.args.split(None, 1)
        pid = int(pid)
        p = db.execute("SELECT id, name FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            return await m.answer(f"Product #{pid} not found.")
        lines = [x.strip() for x in items.splitlines() if x.strip()]
        if not lines:
            return await m.answer("⚠️ No stock items provided.")
        db.executemany("INSERT INTO stock(product_id, payload) VALUES(?,?)", [(pid, x) for x in lines])
        await m.answer(f"Added {len(lines)} item(s) to #{pid} (<b>{esc(p['name'])}</b>). Stock now {stock_count(pid)}.")
    except Exception:
        await m.answer("Usage: /addstock <product_id> <item>  (one item per line)")


@dp.message(Command("refund"), is_admin)
async def admin_refund(m: Message, command: CommandObject = None):
    try:
        if not command or not command.args:
            return await m.answer("Usage: /refund <order_id>")
        oid = int(command.args.strip())
        try:
            db.execute("BEGIN IMMEDIATE")
            o = db.execute("SELECT * FROM orders WHERE id=? AND refunded=0", (oid,)).fetchone()
            if not o:
                db.execute("ROLLBACK")
                return await m.answer("Order not found or already refunded.")
            db.execute("UPDATE orders SET refunded=1, status='refunded' WHERE id=?", (oid,))
            db.execute("COMMIT")
        except Exception:
            try:
                db.execute("ROLLBACK")
            except Exception:
                pass
            raise
        try:
            await m.bot.send_message(
                o["user_id"],
                f"↩️ <b>Refund Processed for Order #{oid}</b>\n\n"
                f"Your order for <b>{esc(o['product_name'])}</b> has been refunded. If you have any questions, please contact Support: {SUPPORT_URL}"
            )
        except Exception:
            pass
        await m.answer(f"Order #{oid} marked as refunded.")
    except (ValueError, TypeError):
        await m.answer("Usage: /refund <order_id>")
    except Exception as e:
        await m.answer(f"Failed to process refund: {esc(str(e))}")


@dp.message(Command("deliver"), is_admin)
async def admin_deliver(m: Message, command: CommandObject = None):
    try:
        if not command or not command.args:
            return await m.answer("Usage: /deliver <order_id> <delivered_content_or_link>")
        oid_str, payload = command.args.split(None, 1)
        oid = int(oid_str)
        o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return await m.answer(f"Order #{oid} not found.")
        if o["refunded"] or o["status"] in ("refunded", "cancelled", "rejected"):
            return await m.answer(f"⚠️ Order #{oid} was refunded or cancelled.")

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
            await m.answer(f"✅ Order #{oid} marked completed, but could not message user: {esc(str(e))}\nPayload: <code>{esc(payload)}</code>")
    except Exception:
        await m.answer("Usage: /deliver <order_id> <delivered_content_or_link>")


@dp.message(Command("stats"), is_admin)
async def admin_stats(m: Message):
    def q(sql):
        return db.execute(sql).fetchone()[0] or 0

    users = q("SELECT COUNT(*) FROM users")
    orders_n = q("SELECT COUNT(*) FROM orders WHERE status='completed' AND refunded=0")
    manual_pending = q("SELECT COUNT(*) FROM orders WHERE status='manual_pending'")
    revenue = q("SELECT SUM(price) FROM orders WHERE status='completed' AND refunded=0")
    pending = q("SELECT COUNT(*) FROM orders WHERE status='pending'")
    admin_curr = get_user_currency(m.from_user.id)
    rev_disp = format_price(revenue, admin_curr)
    await m.answer(
        f"👥 Total buyers: {users}\n"
        f"📦 Completed purchases: {orders_n}\n"
        f"🟡 Awaiting Fulfillment / Model B: {manual_pending}\n"
        f"⏳ Pending checkouts: {pending}\n"
        f"💰 Total revenue: {rev_disp}"
    )


@dp.message(Command("broadcast"), is_admin)
async def admin_broadcast(m: Message, command: CommandObject = None):
    if not command or not command.args:
        return await m.answer("Usage: /broadcast <text>")
    sent = 0
    failed = 0
    for u in db.execute("SELECT id FROM users").fetchall():
        try:
            await m.bot.send_message(u["id"], command.args)
            sent += 1
        except Exception:
            try:
                await m.bot.send_message(u["id"], command.args, parse_mode=None)
                sent += 1
            except Exception:
                failed += 1
        await asyncio.sleep(0.05)
    await m.answer(f"Broadcast completed:\n• Sent: <b>{sent}</b> users\n• Failed: <b>{failed}</b>")


@dp.message(Command("backup"), is_admin)
async def admin_backup(m: Message):
    backup_file = None
    try:
        ts = time.strftime("%Y%m%d_%H%M%S")
        backup_file = f"backup_creditlog_{ts}.db"
        dest = sqlite3.connect(backup_file)
        db.backup(dest)
        dest.close()

        size_kb = os.path.getsize(backup_file) / 1024.0
        doc = FSInputFile(backup_file, filename=f"creditlog_{ts}.db")
        await m.answer_document(
            doc,
            caption=f"💾 <b>Database Backup</b>\n📅 <code>{ts}</code>\n📦 Size: <code>{size_kb:.1f} KB</code>\n\nKeep this file secure!"
        )
    except Exception as e:
        log.exception("Backup creation failed: %s", e)
        await m.answer(f"❌ Failed to create database backup: <code>{esc(str(e))}</code>")
    finally:
        if backup_file and os.path.exists(backup_file):
            try:
                os.remove(backup_file)
            except Exception:
                pass


@dp.message(Command("export"), is_admin)
@dp.message(Command("excel"), is_admin)
async def admin_export_excel(m: Message):
    status_msg = await m.answer("⏳ <i>Generating fresh products Excel catalog...</i>")
    try:
        from generate_catalog_excel import build_catalog_excel
        excel_path = await build_catalog_excel(DB_PATH)
        if not excel_path or not os.path.exists(excel_path):
            excel_path = "CreditLog_Bot_Supplier_Products_Catalog.xlsx"
            if not os.path.exists(excel_path):
                excel_path = "CreditLog_Bot_Supplier_Products_Catalog.csv"

        size_kb = os.path.getsize(excel_path) / 1024.0
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        ext = "csv" if excel_path.endswith(".csv") else "xlsx"
        doc = FSInputFile(excel_path, filename=f"CreditLog_Products_{time.strftime('%Y%m%d_%H%M%S')}.{ext}")
        await m.answer_document(
            doc,
            caption=(
                f"📊 <b>Updated Products Catalog ({ext.upper()})</b>\n"
                f"📅 Generated: <code>{ts}</code>\n"
                f"📦 Size: <code>{size_kb:.1f} KB</code>\n\n"
                f"• Wholesale costs, store prices, profit margins, stock & instructions included."
            )
        )
        await status_msg.delete()
    except Exception as e:
        log.exception("Excel catalog export failed: %s", e)
        await status_msg.edit_text(f"❌ Failed to generate Excel catalog: <code>{esc(str(e))}</code>")


@dp.callback_query(F.data == "admin_export_excel")
async def cb_admin_export_excel(c: CallbackQuery):
    if not (c.from_user and c.from_user.id in ADMIN_IDS):
        return await c.answer("Unauthorized", show_alert=True)
    await c.answer("Generating catalog document...", show_alert=False)
    try:
        from generate_catalog_excel import build_catalog_excel
        excel_path = await build_catalog_excel(DB_PATH)
        if not excel_path or not os.path.exists(excel_path):
            excel_path = "CreditLog_Bot_Supplier_Products_Catalog.xlsx"
            if not os.path.exists(excel_path):
                excel_path = "CreditLog_Bot_Supplier_Products_Catalog.csv"

        size_kb = os.path.getsize(excel_path) / 1024.0
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        ext = "csv" if excel_path.endswith(".csv") else "xlsx"
        doc = FSInputFile(excel_path, filename=f"CreditLog_Products_{time.strftime('%Y%m%d_%H%M%S')}.{ext}")
        await c.message.answer_document(
            doc,
            caption=(
                f"📊 <b>Updated Products Catalog ({ext.upper()})</b>\n"
                f"📅 Generated: <code>{ts}</code>\n"
                f"📦 Size: <code>{size_kb:.1f} KB</code>\n\n"
                f"• Wholesale costs, store prices, profit margins, stock & instructions included."
            )
        )
    except Exception as e:
        log.exception("Excel export callback failed: %s", e)
        await c.message.answer(f"❌ Failed to generate Excel catalog: <code>{esc(str(e))}</code>")


@dp.message(Command("setrate"), is_admin)
async def admin_setrate(m: Message, command: CommandObject = None):
    args = (command.args or "").strip() if command else ""
    current_rate = get_usd_ngn_rate()
    if not args:
        return await m.answer(
            f"💱 <b>USD / NGN Exchange Rate:</b>\n"
            f"• Current rate: <b>₦{current_rate:,} / USD</b>\n\n"
            f"To update, send:\n<code>/setrate &lt;rate_in_naira&gt;</code> (e.g. <code>/setrate 1450</code>)"
        )
    try:
        new_rate = int("".join(c for c in args if c.isdigit()))
        if new_rate <= 0:
            raise ValueError
        set_usd_ngn_rate(new_rate)
        await m.answer(f"✅ USD to NGN exchange rate updated to <b>₦{new_rate:,} / USD</b>.")
    except Exception:
        await m.answer("⚠️ Please specify a valid integer rate, e.g. <code>/setrate 1450</code>")


@dp.message(Command("setbank"), is_admin)
async def admin_setbank(m: Message, command: CommandObject = None):
    args = (command.args or "").strip() if command else ""

    # Disable bank transfers
    if args.lower() in ("off", "disable", "clear", "del", "delete", "none", "0"):
        save_bank_accounts([])
        return await m.answer(
            "✅ <b>Direct Bank Transfer Disabled!</b>\n\n"
            "All bank transfer details have been removed. Direct Bank Transfer will no longer appear in the checkout menu.\n\n"
            "<i>To re-enable at any time, run <code>/setbank &lt;Bank&gt; | &lt;Account Number&gt; | &lt;Account Name&gt;</code></i>"
        )

    # Show current bank accounts if no details provided
    if not args or "|" not in args:
        banks = get_bank_accounts()
        details = get_bank_details()
        configured = is_bank_configured()
        if len(banks) > 1:
            status_text = f"🟢 <b>Configured & Active ({len(banks)} Accounts)</b>"
        elif configured:
            status_text = "🟢 <b>Configured & Active</b>"
        else:
            status_text = "🔴 <b>Not Configured</b>"

        lines = [f"🏦 <b>Direct Bank Transfer Status:</b> {status_text}\n"]
        if banks:
            for idx, b in enumerate(banks, 1):
                num_label = f"Account #{idx}" if len(banks) > 1 else "Primary Account"
                lines.append(
                    f"<b>{num_label}:</b>\n"
                    f"• <b>Bank:</b> {esc(b['bank_name'])}\n"
                    f"• <b>Account Number:</b> <code>{esc(b['account_number'])}</code>\n"
                    f"• <b>Account Name:</b> {esc(b['account_name'])}\n"
                )
        else:
            lines.append(
                f"• <b>Bank:</b> {esc(details['bank_name'])}\n"
                f"• <b>Account Number:</b> <code>{esc(details['account_number']) or 'None'}</code>\n"
                f"• <b>Account Name:</b> {esc(details['account_name'])}\n"
            )

        lines.append(
            "<b>To configure or update your Nigerian bank account:</b>\n"
            "<code>/setbank &lt;Bank Name&gt; | &lt;Account Number&gt; | &lt;Account Name&gt;</code>\n\n"
            "<b>Examples (OPay / PalmPay / Kuda / Moniepoint / Traditional Banks):</b>\n"
            "• <code>/setbank OPay | 8123456789 | Adebayo Olawale</code>\n"
            "• <code>/setbank PalmPay | 9012345678 | Adebayo Olawale</code>\n"
            "• <code>/setbank Kuda | 2012345678 | Adebayo Olawale</code>\n"
            "• <code>/setbank Moniepoint | 6012345678 | Adebayo Olawale</code>\n\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "<b>Multi-Bank & Removal Management:</b>\n"
            "• <b>Add an additional bank account:</b>\n"
            "  <code>/addbank &lt;Bank Name&gt; | &lt;Account Number&gt; | &lt;Account Name&gt;</code>\n"
            "• <b>Remove a specific bank account:</b>\n"
            "  <code>/delbank &lt;account_number_or_id&gt;</code> (e.g. <code>/delbank 2</code>)\n"
            "• <b>Remove ALL bank accounts (disable bank transfer):</b>\n"
            "  <code>/delbank all</code> or <code>/setbank off</code>\n\n"
            "<i>Customers will see Direct Bank Transfer in checkout, transfer Naira directly, and submit their sender name/reference for you to approve with /approve_&lt;order_id&gt;!</i>"
        )
        return await m.answer("\n".join(lines))

    # Parse bank account details
    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 3:
        return await m.answer(
            "⚠️ Please provide all 3 parts separated by vertical bars (|):\n"
            "<code>/setbank &lt;Bank Name&gt; | &lt;Account Number&gt; | &lt;Account Name&gt;</code>\n\n"
            "Example: <code>/setbank OPay | 8123456789 | Adebayo Olawale</code>"
        )

    target_idx = None
    if len(parts) >= 4 and parts[0].isdigit():
        target_idx = int(parts[0]) - 1
        bank_name, acc_num, acc_name = parts[1], parts[2], parts[3]
    else:
        bank_name, acc_num, acc_name = parts[0], parts[1], parts[2]

    clean_num = "".join(c for c in acc_num if c.isdigit())
    if len(clean_num) < 8:
        return await m.answer("⚠️ Account number must contain at least 8 digits.")

    banks = get_bank_accounts()
    new_account = {
        "bank_name": bank_name,
        "account_number": clean_num,
        "account_name": acc_name
    }

    if target_idx is not None and 0 <= target_idx < len(banks):
        banks[target_idx] = new_account
    elif len(banks) > 0 and target_idx is None:
        banks[0] = new_account
    else:
        banks = [new_account]

    save_bank_accounts(banks)

    await m.answer(
        f"✅ <b>Direct Bank Transfer Configured Successfully!</b>\n\n"
        f"• <b>Bank:</b> {esc(bank_name)}\n"
        f"• <b>Account Number:</b> <code>{clean_num}</code>\n"
        f"• <b>Account Name:</b> {esc(acc_name)}\n\n"
        f"🇳🇬 Direct Bank Transfer is now active as a payment option in the checkout menu!"
    )


@dp.message(Command("addbank"), is_admin)
async def admin_addbank(m: Message, command: CommandObject = None):
    args = (command.args or "").strip() if command else ""
    if not args or "|" not in args:
        return await m.answer(
            "<b>Add Another Bank Account:</b>\n"
            "Use this command to add an additional bank account so buyers can choose which bank to pay into.\n\n"
            "<b>Usage:</b>\n"
            "<code>/addbank &lt;Bank Name&gt; | &lt;Account Number&gt; | &lt;Account Name&gt;</code>\n\n"
            "<b>Example:</b>\n"
            "<code>/addbank PalmPay | 9012345678 | Adebayo Olawale</code>"
        )

    parts = [p.strip() for p in args.split("|")]
    if len(parts) < 3:
        return await m.answer(
            "⚠️ Please provide all 3 parts separated by vertical bars (|):\n"
            "<code>/addbank &lt;Bank Name&gt; | &lt;Account Number&gt; | &lt;Account Name&gt;</code>\n\n"
            "Example: <code>/addbank PalmPay | 9012345678 | Adebayo Olawale</code>"
        )

    bank_name, acc_num, acc_name = parts[0], parts[1], parts[2]
    clean_num = "".join(c for c in acc_num if c.isdigit())
    if len(clean_num) < 8:
        return await m.answer("⚠️ Account number must contain at least 8 digits.")

    banks = get_bank_accounts()
    new_account = {
        "bank_name": bank_name,
        "account_number": clean_num,
        "account_name": acc_name
    }
    banks.append(new_account)
    save_bank_accounts(banks)

    await m.answer(
        f"✅ <b>Bank Account #{len(banks)} Added Successfully!</b>\n\n"
        f"• <b>Bank:</b> {esc(bank_name)}\n"
        f"• <b>Account Number:</b> <code>{clean_num}</code>\n"
        f"• <b>Account Name:</b> {esc(acc_name)}\n\n"
        f"Total active bank accounts: <b>{len(banks)}</b>. Buyers will see all available accounts during checkout!"
    )


@dp.message(Command("delbank", "clearbank"), is_admin)
async def admin_delbank(m: Message, command: CommandObject = None):
    args = (command.args or "").strip().lower() if command else ""
    banks = get_bank_accounts()

    if not banks:
        return await m.answer("⚠️ No bank accounts are currently configured.")

    if not args:
        lines = [
            "<b>Remove Bank Account:</b>\n",
            "<b>Configured Accounts:</b>"
        ]
        for idx, b in enumerate(banks, 1):
            lines.append(f"  <b>#{idx}</b>: {esc(b['bank_name'])} — <code>{esc(b['account_number'])}</code> ({esc(b['account_name'])})")
        lines.append(
            "\n<b>Usage:</b>\n"
            "• Remove specific account: <code>/delbank &lt;number&gt;</code> (e.g. <code>/delbank 2</code>)\n"
            "• Remove all accounts (disable bank transfer): <code>/delbank all</code>"
        )
        return await m.answer("\n".join(lines))

    if args in ("all", "clear", "*", "everything"):
        save_bank_accounts([])
        return await m.answer(
            "✅ <b>All Direct Bank Transfer Accounts Removed!</b>\n\n"
            "Direct Bank Transfer has been disabled and will no longer appear in the checkout menu."
        )

    if args.isdigit():
        idx = int(args)
        if 1 <= idx <= len(banks):
            removed = banks.pop(idx - 1)
            save_bank_accounts(banks)
            rem_text = f"Total remaining active bank accounts: <b>{len(banks)}</b>" if banks else "No accounts left. Direct Bank Transfer is now disabled."
            return await m.answer(
                f"✅ <b>Removed Bank Account #{idx}:</b>\n"
                f"• <b>Bank:</b> {esc(removed['bank_name'])}\n"
                f"• <b>Account Number:</b> <code>{esc(removed['account_number'])}</code>\n\n"
                f"{rem_text}"
            )
        else:
            return await m.answer(f"⚠️ Invalid account number. Please choose between 1 and {len(banks)} (or send <code>/delbank all</code>).")

    # Try matching by account number
    clean_arg = "".join(c for c in args if c.isdigit())
    if clean_arg:
        matching = [i for i, b in enumerate(banks) if clean_arg in b["account_number"]]
        if len(matching) == 1:
            removed = banks.pop(matching[0])
            save_bank_accounts(banks)
            return await m.answer(
                f"✅ <b>Removed Bank Account:</b>\n"
                f"• <b>Bank:</b> {esc(removed['bank_name'])}\n"
                f"• <b>Account Number:</b> <code>{esc(removed['account_number'])}</code>\n\n"
                f"Remaining active accounts: {len(banks)}"
            )

    await m.answer("Usage: <code>/delbank &lt;number&gt;</code> (e.g. <code>/delbank 2</code>) or <code>/delbank all</code>")


@dp.message(Command("bank", "banks"), is_admin)
async def admin_view_bank(m: Message, command: CommandObject = None):
    return await admin_setbank(m, command)


@dp.message(Command("setbachs"), is_admin)
async def admin_setbachs(m: Message, command: CommandObject = None):
    args = (command.args or "").strip() if command else ""
    if not args:
        configured = is_bachs_configured()
        status_text = "🟢 <b>Configured & Active</b>" if configured else "🔴 <b>Not Configured</b>"
        key = get_bachs_key()
        masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else ("(not set)" if not key else "***")
        curr = get_bachs_currency()
        env_type = "Sandbox" if "sandbox" in key.lower() or key.startswith("sk_test") else "Live"
        return await m.answer(
            f"💳 <b>Bachs Payment Gateway Status:</b> {status_text}\n\n"
            f"• <b>API Key:</b> <code>{masked}</code>\n"
            f"• <b>Environment:</b> <code>{env_type}</code>\n"
            f"• <b>Currency:</b> <code>{curr}</code>\n"
            f"• <b>Base URL:</b> <code>{get_bachs_base_url()}</code>\n\n"
            f"<b>To configure or update your Bachs API key:</b>\n"
            f"<code>/setbachs &lt;api_key&gt;</code>\n\n"
            f"<i>Get your API key (sk_live_... or sk_sandbox_...) from your dashboard at https://app.bachs.io</i>"
        )
    set_setting("bachs_api_key", args)
    env_type = "Sandbox" if "sandbox" in args.lower() or args.startswith("sk_test") else "Live"
    await m.answer(
        f"✅ <b>Bachs API Key Updated!</b>\n\n"
        f"• <b>Environment:</b> <code>{env_type}</code>\n"
        f"• <b>Base URL:</b> <code>{get_bachs_base_url(args)}</code>\n\n"
        f"💳 Customers can now pay using Cards, Bank Transfer, Mobile Money, and Crypto via Bachs in checkout!"
    )


@dp.message(Command("bachs"), is_admin)
async def admin_view_bachs(m: Message, command: CommandObject = None):
    return await admin_setbachs(m, command)


@dp.message(Command("bachscurrency"), is_admin)
async def admin_bachs_currency(m: Message, command: CommandObject = None):
    args = (command.args or "").strip().upper() if command else ""
    if args not in ("USD", "NGN", "GHS", "KES", "ZAR", "EUR", "GBP"):
        curr = get_bachs_currency()
        return await m.answer(
            f"💱 <b>Bachs Currency Setting:</b> <code>{curr}</code>\n\n"
            f"To change the currency billed through Bachs checkout sessions, send:\n"
            f"<code>/bachscurrency USD</code> or <code>/bachscurrency NGN</code>\n\n"
            f"<i>Note: In USD mode, Bachs adaptive checkout still presents local currency options to African buyers automatically.</i>"
        )
    set_setting("bachs_currency", args)
    await m.answer(f"✅ Bachs checkout currency updated to <b>{args}</b>.")




# Supplier commands
@dp.message(Command("supplier", "suppliers"), is_admin)
async def admin_supplier_info(m: Message, command: CommandObject = None):
    registered = supplier_registry.list_suppliers()
    if not registered and not RESELLER_API_KEY:
        return await m.answer("⚠️ <code>RESELLER_API_KEY</code> is not configured in .env.")

    arg = (command.args or "").strip().lower() if command else ""
    client = None

    if arg:
        if arg in registered:
            client = registered[arg]
        else:
            avail = ", ".join(f"<code>{esc(k)}</code>" for k in registered.keys())
            return await m.answer(
                f"⚠️ Supplier '<code>{esc(arg)}</code>' not found in registry.\n"
                f"<b>Configured Suppliers:</b> {avail}\n\n"
                f"<i>Tip: Use <code>/supplier</code> to view the primary supplier.</i>"
            )
    else:
        def_name = supplier_registry.default_name() if hasattr(supplier_registry, "default_name") else "default"
        client = registered.get(def_name) or registered.get("default") or reseller

    if not client:
        return await m.answer("⚠️ Supplier client not available. Please check .env configuration.")

    has_key = bool(client.api_key and not client.api_key.startswith("your_"))

    try:
        bal_res = await client.get_balance()
        b_val = bal_res.get('balance', 0)
        try:
            b_float = float(b_val) if b_val is not None else 0.0
            bal_str = f"${b_float:.2f} {bal_res.get('currency', 'USD')}"
        except (ValueError, TypeError):
            bal_str = f"{b_val} {bal_res.get('currency', 'USD')}"
    except Exception as e:
        bal_str = f"Error: {e}"

    try:
        info_res = await client.get_info()
        info_text = json.dumps(info_res, indent=2, ensure_ascii=False)
        if len(info_text) > 1500:
            info_text = info_text[:1500] + "..."
    except Exception as e:
        info_text = f"Error: {e}"

    lines = [
        "🌐 <b>Supplier API Status</b>",
        f"Base URL: {esc(client.base_url)}",
        f"API Key: {'Set (✓)' if has_key else 'Missing (✗)'}",
        f"Supplier Balance: {esc(bal_str)}\n",
        "<b>Account Info:</b>",
        f'<pre><code class="language-json">{esc(info_text)}</code></pre>',
    ]
    await m.answer("\n".join(lines))


@dp.message(Command("mode"), is_admin)
async def admin_mode(m: Message, command: CommandObject = None):
    """View or change fulfillment mode (instant vs manual approval)."""
    arg = command.args.strip().lower() if command and command.args else None
    if arg:
        if arg in ("auto", "automatic"):
            set_setting("fulfillment_mode", "auto")
            return await m.answer("✅ Fulfillment mode set to <b>AUTO (Dynamic)</b>.\n• Model A (Instant delivery) when supplier balance > 0.\n• Falls back to Model B (Manual top-up & /approve) when supplier balance is insufficient.")
        elif arg in ("manual", "b", "model_b"):
            set_setting("fulfillment_mode", "manual")
            return await m.answer("✅ Fulfillment mode set to <b>MODEL B (Manual)</b>.\n• All automated supplier deliveries are paused.\n• All orders wait for you to top up and /approve.")
        elif arg in ("instant", "a", "model_a"):
            set_setting("fulfillment_mode", "instant")
            return await m.answer("✅ Fulfillment mode set to <b>MODEL A (Instant)</b>.\n• Bot will always attempt instant supplier fulfillment.")
        else:
            return await m.answer("<b>Usage:</b>\n• <code>/mode auto</code> - Dynamic (Model A if funded, Model B if empty)\n• <code>/mode manual</code> - Force Model B (pause instant deliveries)\n• <code>/mode instant</code> - Force Model A (always instant)")

    current_mode = get_setting("fulfillment_mode", "auto").lower()
    bal = 0.0
    try:
        sup = supplier_registry.get() or reseller
        bal_res = await sup.get_balance()
        b_val = bal_res.get("balance", 0.0)
        bal = float(b_val) if b_val is not None else 0.0
    except Exception as e:
        log.warning("Could not fetch balance in /mode: %s", e)

    active_model = "Model A (Instant Auto-Delivery)" if (current_mode == "instant" or (bal > 0 and current_mode != "manual")) else "Model B (Manual Top-Up & /approve)"
    await m.answer(
        f"⚙️ <b>Store Fulfillment Strategy</b>\n\n"
        f"• <b>Configured Mode:</b> <code>{current_mode.upper()}</code>\n"
        f"• <b>Current Supplier Balance:</b> <b>${bal:.2f} USDT</b>\n"
        f"• <b>Currently Active:</b> <b>{active_model}</b>\n\n"
        f"<i>When supplier balance is insufficient for an order, automated delivery pauses so you can use customer funds to top up before approving.</i>\n\n"
        f"<b>Switch Mode:</b>\n"
        f"• <code>/mode auto</code> - Dynamic (Recommended)\n"
        f"• <code>/mode manual</code> - Force Model B (Pause all auto-delivery)\n"
        f"• <code>/mode instant</code> - Force Model A (Instant only)"
    )


@dp.message(Command("s_products"), is_admin)
async def admin_supplier_products(m: Message, command: CommandObject = None):
    sup_name = ((command.args or "").strip().lower() if command else "") or supplier_registry.default_name()
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

    admin_curr = get_user_currency(m.from_user.id)
    lines = [f"<b>Supplier '{esc(sup_name)}' Catalog Products (Currency: {admin_curr}):</b>\n"]
    for p in products:
        pid = p["id"]
        linked = local_map.get(pid)
        if linked:
            local_price_disp = format_price(linked['price'], admin_curr)
            status = f"✅ In shop (Local #{linked['id']}, {local_price_disp})"
        else:
            status = "⚪ Not linked"

        cost_disp = format_price(p['price_cents'], admin_curr)
        stock_disp = f", stock: {p['stock']}" if p.get("stock") is not None else ""
        lines.append(
            f"• <code>{esc(pid)}</code>: <b>{esc(p['name'])}</b>\n"
            f"  Cost: {cost_disp}{stock_disp} · {status}"
        )

    for chunk in chunk_lines(lines):
        await m.answer(chunk)


async def perform_supplier_sync(
    target_suppliers: Optional[List[str]] = None,
    markup: Optional[int] = None,
) -> Tuple[int, int, int, List[str]]:
    """
    Synchronizes catalog and live inventory from reseller API.
    Updates retail prices with markup, updates supplier_stock, sets auto delivery,
    and reconciles unlisted or depleted items as out of stock.
    Returns: (total_added, total_updated, total_items, errors)
    """
    if markup is None:
        markup = get_default_margin()

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
                "SELECT id, price, margin, fixed_price FROM products WHERE supplier_product_id=? AND supplier=?",
                (sp_id, sup_name)
            ).fetchone()

            # Try matching by name if ID didn't match
            if not existing:
                existing = db.execute(
                    "SELECT id, price, margin, fixed_price FROM products WHERE supplier_product_id IS NULL AND LOWER(name)=LOWER(?)",
                    (sp["name"],)
                ).fetchone()

            if not existing:
                clean_sp_name = sp["name"].lower()
                if "gemini" in clean_sp_name and "18" in clean_sp_name:
                    existing = db.execute("SELECT id, price, margin, fixed_price FROM products WHERE supplier_product_id IS NULL AND name LIKE '%Gemini%18%'").fetchone()
                elif "duolingo" in clean_sp_name and ("12" in clean_sp_name or "super" in clean_sp_name):
                    existing = db.execute("SELECT id, price, margin, fixed_price FROM products WHERE supplier_product_id IS NULL AND name LIKE '%Duolingo%'").fetchone()

            is_fixed = bool(existing and ("fixed_price" in existing.keys()) and existing["fixed_price"])
            is_active = 0 if (stock is not None and stock == 0) else 1

            if is_fixed:
                # Keep retail price fixed and adjust margin
                retail_cents = existing["price"]
                if cost_cents and cost_cents > 0:
                    item_margin = round(((retail_cents - cost_cents) / cost_cents) * 100)
                else:
                    item_margin = existing["margin"]
                db_margin = item_margin
                db_fixed_price = 1
            else:
                # Update retail price using margin
                saved_margin = existing["margin"] if existing else None
                item_margin = saved_margin if (saved_margin is not None) else markup
                retail_cents = max(100, round(cost_cents * (1 + item_margin / 100)))
                db_margin = saved_margin
                db_fixed_price = 0

            if existing:
                db.execute("""
                    UPDATE products
                    SET name=?, description=?, price=?, supplier_price=?, auto_fulfill=1, active=?, category=?, supplier=?,
                        supplier_stock=?, delivery_type='auto', supplier_product_id=?, margin=?, fixed_price=?
                    WHERE id=?
                """, (sp["name"], sp["description"], retail_cents, cost_cents, is_active, cat, sup_name, stock, sp_id, db_margin, db_fixed_price, existing["id"]))
                total_updated += 1
            else:
                db.execute("""
                    INSERT INTO products(name, description, price, supplier_product_id, supplier_price, auto_fulfill, active, category, supplier, supplier_stock, delivery_type, margin, fixed_price)
                    VALUES(?,?,?,?,?,1,?,?,?,?, 'auto', ?, 0)
                """, (sp["name"], sp["description"], retail_cents, sp_id, cost_cents, is_active, cat, sup_name, stock, None))
                total_added += 1

        # Deactivate supplier products no longer in feed
        if seen_supplier_ids:
            placeholders = ",".join(["?"] * len(seen_supplier_ids))
            db.execute(
                f"UPDATE products SET supplier_stock=0, active=0 WHERE supplier=? AND delivery_type='auto' AND supplier_product_id NOT IN ({placeholders})",
                [sup_name] + list(seen_supplier_ids)
            )

        total_items += len(sup_products)

    return total_added, total_updated, total_items, errors


@dp.message(Command("s_sync"), is_admin)
async def admin_supplier_sync(m: Message, command: CommandObject = None):
    registered = supplier_registry.list_suppliers()
    if not registered:
        return await m.answer("⚠️ No suppliers configured in .env.")

    args = (command.args or "").strip().split() if command else []
    target_suppliers = []
    markup = get_default_margin()

    if not args:
        target_suppliers = [supplier_registry.default_name()]
    elif args[0].lower() == "all":
        target_suppliers = list(registered.keys())
        if len(args) > 1:
            markup = parse_profit_margin(args[1], default=markup)
            set_setting("profit_margin", str(markup))
    elif args[0].lower() in registered:
        target_suppliers = [args[0].lower()]
        if len(args) > 1:
            markup = parse_profit_margin(args[1], default=markup)
            set_setting("profit_margin", str(markup))
    else:
        markup = parse_profit_margin(args[0], default=markup)
        set_setting("profit_margin", str(markup))
        target_suppliers = [supplier_registry.default_name()]

    total_added, total_updated, total_items, errors = await perform_supplier_sync(target_suppliers, markup)

    # Group products by status
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

    admin_curr = get_user_currency(m.from_user.id)
    lines = [
        f"🔄 <b>Catalog Sync & Health Report ({admin_curr})</b>\n",
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
            lines.append(f" • <code>#{p['id']}</code> {esc(p['name'])} · {format_price(p['price'], admin_curr)}{s_disp}")
        if len(in_stock_auto) > 8:
            lines.append(f" <i>...and {len(in_stock_auto) - 8} more in-stock items</i>\n")
        else:
            lines.append("")

    if manual_items:
        lines.append("🟡 <b>Manual / Assisted Services:</b>")
        for p in manual_items[:8]:
            lines.append(f" • <code>#{p['id']}</code> {esc(p['name'])} · {format_price(p['price'], admin_curr)}")
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
    sync_kb = kb([[btn("📊 Download Excel Catalog (.xlsx)", "admin_export_excel", style="success")]])
    await m.answer(res_msg, reply_markup=sync_kb)

    if "unittest" not in sys.modules:
        try:
            from generate_catalog_excel import build_catalog_excel
            asyncio.create_task(build_catalog_excel(DB_PATH))
        except Exception:
            pass


@dp.message(Command("s_link"), is_admin)
async def admin_supplier_link(m: Message, command: CommandObject = None):
    try:
        if not command or not command.args:
            return await m.answer("Usage: /s_link <local_product_id> <supplier_product_id> [supplier_name]")
        parts = command.args.split()
        local_id = int(parts[0])
        sup_id = parts[1].strip()
        sup_name = parts[2].strip().lower() if len(parts) > 2 else supplier_registry.default_name()

        p = db.execute("SELECT * FROM products WHERE id=?", (local_id,)).fetchone()
        if not p:
            return await m.answer(f"Product #{local_id} not found.")

        db.execute(
            "UPDATE products SET supplier_product_id=?, supplier=?, auto_fulfill=1, delivery_type='auto' WHERE id=?",
            (sup_id, sup_name, local_id)
        )
        await m.answer(f"Product #{local_id} ({esc(p['name'])}) linked to supplier '<b>{esc(sup_name)}</b>' product <code>{esc(sup_id)}</code>.")
    except Exception:
        await m.answer("Usage: /s_link <local_product_id> <supplier_product_id> [supplier_name]")


@dp.message(Command("s_unlink"), is_admin)
async def admin_supplier_unlink(m: Message, command: CommandObject = None):
    try:
        if not command or not command.args:
            return await m.answer("Usage: /s_unlink <local_product_id>")
        local_id = int(command.args.strip())
        p = db.execute("SELECT * FROM products WHERE id=?", (local_id,)).fetchone()
        if not p:
            return await m.answer(f"Product #{local_id} not found.")
        db.execute("UPDATE products SET supplier_product_id=NULL, auto_fulfill=0, delivery_type='manual' WHERE id=?", (local_id,))
        await m.answer(f"Product #{local_id} ({esc(p['name'])}) unlinked from supplier. Now uses local stock.")
    except Exception:
        await m.answer("Usage: /s_unlink <local_product_id>")


@dp.message(Command("s_orders"), is_admin)
async def admin_supplier_orders(m: Message, command: CommandObject = None):
    sup_name = (command.args or "").strip().lower() if (command and command.args) else ""
    client = (supplier_registry.get(sup_name) if sup_name else supplier_registry.get()) or reseller
    if not client or not client.api_key:
        target = f" for '{esc(sup_name)}'" if sup_name else ""
        return await m.answer(f"⚠️ Supplier client{target} is not configured or missing API key.")
    try:
        orders = await client.get_orders()
        if not orders:
            target = f" ({esc(sup_name)})" if sup_name else ""
            return await m.answer(f"No orders found at supplier{target}.")
        sup_label = f" ({esc(sup_name)})" if sup_name else ""
        lines = [f"<b>Recent Supplier Orders{sup_label}:</b>\n"]
        for o in orders[:10]:
            code = o.get("orderCode") or o.get("id") or "N/A"
            item = o.get("productId") or o.get("product_name") or "Item"
            cost = o.get("price") or o.get("cost") or ""
            lines.append(f"• <code>{esc(str(code))}</code> - {esc(str(item))} {esc(str(cost))}")
        await m.answer("\n".join(lines))
    except Exception as e:
        await m.answer(f"Failed to fetch supplier orders: {esc(str(e))}")


async def periodic_supplier_sync(interval_sec: int = 600):
    """Background task to sync catalog every 10 minutes."""
    while True:
        try:
            await asyncio.sleep(interval_sec)
            added, updated, total, errors = await perform_supplier_sync()
            log.info("Periodic supplier sync completed: %d updated, %d added of %d total (errors: %s)", updated, added, total, len(errors))
            try:
                from generate_catalog_excel import build_catalog_excel
                await build_catalog_excel(DB_PATH)
            except Exception as ex:
                log.warning("Periodic Excel catalog refresh failed: %s", ex)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("Periodic supplier sync encountered error: %s", e)


# Main entrypoint
async def main():
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        bot_info = await bot.get_me()
        if bot_info and bot_info.username:
            set_setting("bot_username", bot_info.username)
    except Exception:
        pass
    app = web.Application()
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Health check server listening on :%s", PORT)

    # Sync catalog on startup
    try:
        init_added, init_updated, init_total, init_errs = await perform_supplier_sync()
        log.info("Initial supplier sync: %d updated, %d added of %d total (errors: %d)", init_updated, init_added, init_total, len(init_errs))
        try:
            from generate_catalog_excel import build_catalog_excel
            await build_catalog_excel(DB_PATH)
        except Exception:
            pass
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
            BotCommand(command="currency", description="Switch currency (USD/NGN)"),
            BotCommand(command="lang", description="Switch language"),
            BotCommand(command="support", description="Contacts support"),
        ])
        admin_cmd_list = [
            BotCommand(command="admin", description="Admin command center"),
            BotCommand(command="mode", description="Switch Model A / Model B fulfillment"),
            BotCommand(command="backup", description="Download database backup (.db)"),
            BotCommand(command="export", description="Download updated products Excel (.xlsx)"),
            BotCommand(command="stats", description="View store sales stats"),
            BotCommand(command="products", description="List products & margins"),
            BotCommand(command="s_products", description="List supplier products"),
            BotCommand(command="s_sync", description="Sync supplier catalog"),
            BotCommand(command="s_orders", description="Recent supplier orders"),
            BotCommand(command="supplier", description="Supplier balance & info"),
            BotCommand(command="setrate", description="Update USD/NGN exchange rate"),
            BotCommand(command="setbachs", description="Configure Bachs API Key"),
            BotCommand(command="bachs", description="View Bachs Gateway details"),
            BotCommand(command="setbank", description="Configure Direct Bank Transfer"),
            BotCommand(command="addbank", description="Add additional bank account"),
            BotCommand(command="delbank", description="Remove a bank account"),
            BotCommand(command="bank", description="View Bank Transfer details"),
            BotCommand(command="fsub", description="Manage Force-Subscribe"),
            BotCommand(command="broadcast", description="Broadcast message to buyers"),
        ]
        for aid in ADMIN_IDS:
            try:
                await bot.set_my_commands(admin_cmd_list, scope=BotCommandScopeChat(chat_id=aid))
            except Exception as e:
                log.debug("Could not set admin commands for chat %s: %s", aid, e)

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
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
