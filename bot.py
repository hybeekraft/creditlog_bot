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
import re
import sqlite3
import sys
import time
from typing import Dict, List, Optional, Tuple

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
from web_api import register_web_routes, TELEGRAM_BOT_KEY

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("store")
logger = log

# Bot settings and environment
TOKEN = os.environ.get("BOT_TOKEN", "").strip()
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
RESELLER_BASE_URL = (os.environ.get("RESELLER_BASE_URL") or "https://api.renqis.online/api/reseller/v1").rstrip("/")
RESELLER_API_KEY = os.environ.get("RESELLER_API_KEY", "")
supplier_registry = SupplierRegistry.from_env()
reseller = supplier_registry.get("default") or ResellerClient(base_url=RESELLER_BASE_URL, api_key=RESELLER_API_KEY)
if "default" not in supplier_registry.list_suppliers():
    supplier_registry.register("default", reseller, is_default=True)

# Crypto payment settings
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
                if res:
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


def is_bachs_enabled() -> bool:
    """Check if Bachs payments are active (not paused)."""
    return get_setting("bachs_enabled", "1") not in ("0", "false", "paused", "off")


def is_broadcaster_enabled() -> bool:
    """Check if automated stock broadcaster is enabled (not paused)."""
    return get_setting("broadcaster_enabled", "1") not in ("0", "false", "paused", "off")


def is_bachs_configured() -> bool:
    """Check if Bachs is set up and active."""
    if not is_bachs_enabled():
        return False
    key = get_bachs_key()
    if key.lower() in ("off", "disable", "disabled", "none", "0"):
        return False
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

# Default store categories and icons (matching supplier retail bot layout)
DEFAULT_CATEGORIES = [
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
    ("deepseek", "DeepSeek", "🐳"),
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
    ("muse_ai", "Muse AI", "Ⓜ️"),
    ("lovable", "Lovable", "❤️"),
    ("capcut", "CapCut", "✂️"),
]
CATEGORIES = list(DEFAULT_CATEGORIES)
CAT_BY_KEY = {k: (name, icon) for k, name, icon in CATEGORIES}
CAT_KEY_BY_NAME = {name.lower(): k for k, name, icon in CATEGORIES}


def reload_categories():
    """Reload categories from database table if present, else seed defaults."""
    global CATEGORIES, CAT_BY_KEY, CAT_KEY_BY_NAME
    try:
        if db:
            cur = db.cursor()
            cur.execute("""
                CREATE TABLE IF NOT EXISTS categories (
                    key TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    icon TEXT NOT NULL,
                    sort_order INTEGER DEFAULT 0
                )
            """)
            rows = cur.execute("SELECT key, name, icon FROM categories ORDER BY sort_order ASC, rowid ASC").fetchall()
            if not rows:
                for idx, (k, n, ic) in enumerate(DEFAULT_CATEGORIES):
                    cur.execute(
                        "INSERT OR IGNORE INTO categories (key, name, icon, sort_order) VALUES (?, ?, ?, ?)",
                        (k, n, ic, idx)
                    )
                db.commit()
                rows = cur.execute("SELECT key, name, icon FROM categories ORDER BY sort_order ASC, rowid ASC").fetchall()

            if rows:
                CATEGORIES.clear()
                for r in rows:
                    CATEGORIES.append((r["key"], r["name"], r["icon"]))
                CAT_BY_KEY.clear()
                CAT_BY_KEY.update({k: (name, icon) for k, name, icon in CATEGORIES})
                CAT_KEY_BY_NAME.clear()
                CAT_KEY_BY_NAME.update({name.lower(): k for k, name, icon in CATEGORIES})
    except Exception as e:
        logger.warning(f"Could not load categories table: {e}")


CATEGORY_KEYWORDS = (
    (("codex", "astra"), "Codex API"),
    (("chatgpt", "gpt", "openai"), "ChatGPT"),
    (("gemini", "antigravity", "argon", "hydrogen", "google ai", "google one", "google pro"), "Gemini"),
    (("grok",), "Grok"),
    (("claude", "anthropic"), "Claude"),
    (("kimi", "moonshot"), "Kimi"),
    (("capcut",), "CapCut"),
    (("muse ai", "muse"), "Muse AI"),
    (("lovable", "loveable"), "Lovable"),
    (("sora", "runway", "pika", "kling", "luma", "video ai", "heygen", "veo", "higgsfield", "pixverse", "seedance"), "Video AI"),
    (("elevenlabs", "eleven"), "ElevenLabs"),
    (("qwen", "tongyi"), "Qwen API"),
    (("deepseek",), "DeepSeek"),
    (("cursor",), "Cursor API"),
    (("adobe", "photoshop", "illustrator"), "Adobe"),
    (("autodesk", "autocad", "revit", "3ds max", "maya"), "Autodesk"),
    (("canva", "figma", "freepik", "envato", "design", "midjourney", "krea", "picsart", "gamma", "mobbin", "leonardo", "flux", "openart"), "Design"),
    (("office", "windows", "microsoft", "o365", "excel", "word", "zoom", "notion", "ilovepdf", "icloud"), "Office"),
    (("duolingo", "coursera", "udemy", "education", "chegg", "quizlet", "cambly", "grammarly", "quillbot", "scribd", "jetbrains", "student", "kiro"), "Education"),
    (("netflix", "disney", "hbo", "prime video", "prime", "streaming", "hulu", "crunchyroll", "youtube"), "Streaming"),
    (("suno", "udio", "spotify", "apple music", "tidal", "deezer", "music", "pocket fm"), "Music"),
    (("vpn", "nordvpn", "expressvpn", "surfshark", "mullvad", "hma", "proton", "windscribe"), "VPN"),
    (("security", "antivirus", "malwarebytes", "kaspersky", "eset", "mcafee", "avast"), "Security"),
    (("steam", "playstation", "xbox", "nintendo", "game", "roblox", "fallout"), "Games"),
    (("twitter", "x.com", "instagram", "tiktok", "facebook", "linkedin", "social"), "Social"),
    (("discord", "nitro", "decor", "gifting"), "Discord"),
)

PRODUCT_ICON_KEYWORDS = (
    (("snapchat",), "👻"),
    (("cursor",), "🔲"),
    (("elevenlabs", "eleven"), "⏸️"),
    (("kimi",), "🐇"),
    (("capcut",), "✂️"),
    (("kling", "veo", "heygen", "higgsfield"), "🎥"),
    (("autodesk", "autocad"), "🅰️"),
    (("prime video", "prime", "apple tv"), "📺"),
    (("fallout", "game pass", "xbox"), "🎮"),
    (("avast", "mcafee"), "🛡️"),
    (("gamma", "wink", "gemini", "antigravity", "argon", "hydrogen", "google"), "✨"),
    (("framer", "magic patterns", "mobbin", "supercut", "krea", "leonardo", "figma", "openart"), "🎨"),
    (("n8n",), "⚙️"),
    (("brain.fm",), "🎵"),
    (("customer.io",), "📬"),
    (("fin ai", "manus"), "🤖"),
    (("warp", "jetbrains"), "💻"),
    (("readwise",), "📖"),
    (("icloud",), "☁️"),
    (("wordwall", "quizlet", "scribd", "ilovepdf"), "📚"),
    (("notion",), "📝"),
    (("qwen",), "🔮"),
    (("deepseek",), "🐳"),
    (("surfshark",), "🦈"),
    (("discord", "nitro"), "👾"),
    (("gmail",), "📧"),
    (("grammarly", "quillbot"), "✍️"),
    (("pocket fm",), "📻"),
    (("flux",), "⚡"),
    (("hbo",), "🎬"),
    (("windows",), "🪟"),
    (("coursera", "kiro"), "🎓"),
    (("peacock",), "🦚"),
)


def detect_category(name: str) -> str:
    n = name.lower()
    for keywords, category in CATEGORY_KEYWORDS:
        if any(k in n for k in keywords):
            return category
    return "AI Tools"


def get_product_icon(name: str, cat_name: Optional[str] = None) -> str:
    n = name.lower()
    for keywords, icon in PRODUCT_ICON_KEYWORDS:
        if any(k in n for k in keywords):
            return icon
    category = cat_name or detect_category(name)
    key = CAT_KEY_BY_NAME.get(category.lower())
    if key and key in CAT_BY_KEY:
        return CAT_BY_KEY[key][1]
    return "📦"


def get_products_for_category(key: str) -> list[sqlite3.Row]:
    cat_name, _ = CAT_BY_KEY.get(key, ("AI Tools", "⚡"))
    all_prods = db.execute("SELECT * FROM products ORDER BY id").fetchall()
    matched = []
    target_names = {cat_name.lower(), key.lower()}
    if cat_name.lower() == "deepseek":
        target_names.add("deepseek ai")
    elif cat_name.lower() == "deepseek ai":
        target_names.add("deepseek")
    for p in all_prods:
        p_cat = (p["category"] or "").strip().lower()
        if p_cat in target_names:
            matched.append(p)
        elif not p_cat or p_cat == "ai tools":
            if detect_category(p["name"]).lower() in target_names:
                matched.append(p)

    # Deduplicate by normalized name: prefer auto-fulfill / default, then active/in-stock, then lowest id
    by_norm = {}
    for p in matched:
        norm = re.sub(r"[^a-z0-9]", "", p["name"].lower())
        if norm not in by_norm:
            by_norm[norm] = p
        else:
            prev = by_norm[norm]
            prev_score = (2 if (prev["auto_fulfill"] or prev["supplier"] == "default") else 0) + (1 if prev["active"] else 0)
            curr_score = (2 if (p["auto_fulfill"] or p["supplier"] == "default") else 0) + (1 if p["active"] else 0)
            if curr_score > prev_score:
                by_norm[norm] = p

    seen_ids = {p["id"] for p in by_norm.values()}
    return [p for p in matched if p["id"] in seen_ids]


def deduplicate_products_in_db(conn):
    """Finds and merges duplicate products in products table by normalized name."""
    try:
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, name, description, price, supplier_price, auto_fulfill, active, supplier, supplier_stock FROM products ORDER BY id ASC"
        ).fetchall()
        groups = {}
        for r in rows:
            p_name = r[1] if isinstance(r, tuple) else r["name"]
            norm = re.sub(r"[^a-z0-9]", "", str(p_name).lower())
            groups.setdefault(norm, []).append(r)

        deleted_count = 0
        for norm, plist in groups.items():
            if len(plist) <= 1:
                continue

            def _sort_key(p):
                auto = p[5] if isinstance(p, tuple) else p["auto_fulfill"]
                sup = p[7] if isinstance(p, tuple) else p["supplier"]
                act = p[6] if isinstance(p, tuple) else p["active"]
                pid = p[0] if isinstance(p, tuple) else p["id"]
                # Prefer auto_fulfill / default, then active, then lowest ID
                is_auto = 1 if (auto or sup == "default") else 0
                is_act = 1 if act else 0
                return (is_auto, is_act, -pid)

            plist_sorted = sorted(plist, key=_sort_key, reverse=True)
            canonical = plist_sorted[0]
            duplicates = plist_sorted[1:]

            canon_id = canonical[0] if isinstance(canonical, tuple) else canonical["id"]
            best_desc = (canonical[2] if isinstance(canonical, tuple) else canonical["description"]) or ""

            for d in duplicates:
                d_desc = (d[2] if isinstance(d, tuple) else d["description"]) or ""
                if len(d_desc) > len(best_desc):
                    best_desc = d_desc

            orig_canon_desc = (canonical[2] if isinstance(canonical, tuple) else canonical["description"]) or ""
            if best_desc != orig_canon_desc:
                cur.execute("UPDATE products SET description=? WHERE id=?", (best_desc, canon_id))

            for d in duplicates:
                dup_id = d[0] if isinstance(d, tuple) else d["id"]
                cur.execute("UPDATE orders SET product_id=? WHERE product_id=?", (canon_id, dup_id))
                cur.execute("DELETE FROM products WHERE id=?", (dup_id,))
                deleted_count += 1

        if deleted_count > 0:
            conn.commit()
            logger.info("Deduplicated database products: removed %d duplicate rows.", deleted_count)
        return deleted_count
    except Exception as e:
        logger.warning(f"Error deduplicating products: {e}")
        return 0


def migrate_db():
    table_cols = {
        "products": [
            ("supplier_product_id", "TEXT DEFAULT NULL"),
            ("supplier_price", "INTEGER DEFAULT NULL"),
            ("auto_fulfill", "INTEGER DEFAULT 0"),
            ("margin", "INTEGER DEFAULT NULL"),
            ("category", "TEXT DEFAULT 'AI Tools'"),
            ("supplier", "TEXT DEFAULT 'default'"),
            ("supplier_stock", "INTEGER DEFAULT NULL"),
            ("delivery_type", "TEXT DEFAULT 'auto'"),
        ],
        "orders": [
            ("supplier_order_id", "TEXT DEFAULT NULL"),
            ("supplier", "TEXT DEFAULT 'default'"),
            ("status", "TEXT DEFAULT 'completed'"),
            ("invoice_id", "TEXT DEFAULT NULL"),
            ("quantity", "INTEGER DEFAULT 1"),
            ("payment_method", "TEXT DEFAULT NULL"),
            ("txid", "TEXT DEFAULT NULL"),
            ("expected_amount", "TEXT DEFAULT NULL"),
        ],
        "users": [
            ("currency", "TEXT DEFAULT 'USD'"),
        ],
    }
    for table, cols in table_cols.items():
        existing = {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}
        for col_name, col_def in cols:
            if col_name not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_def}")

    # Set categories for older products if missing
    for row in db.execute("SELECT id, name, category FROM products").fetchall():
        if not row["category"] or row["category"] == "AI Tools":
            detected = detect_category(row["name"])
            if detected != "AI Tools":
                db.execute("UPDATE products SET category=? WHERE id=?", (detected, row["id"]))

    # Products catalog is populated dynamically from the supplier API via perform_supplier_sync()

    # Brand custom emoji table
    db.execute("""
        CREATE TABLE IF NOT EXISTS custom_emojis(
          key TEXT PRIMARY KEY,
          emoji_id TEXT NOT NULL,
          created TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)

    # Reload store categories from database
    reload_categories()

    # One-time safe product ID resequencing to clean 1..N
    if get_setting("products_resequenced_v1") != "1":
        resequence_product_ids(db)
        set_setting("products_resequenced_v1", "1")

    # Sync retail catalog SKUs, live stock, and rich descriptions from retail_catalog_details.json
    try:
        from retail_catalog import sync_retail_catalog_details
        sync_retail_catalog_details(db)
        resequence_product_ids(db)
    except Exception as e:
        logger.warning(f"Retail details sync skipped: {e}")

    # Clean up any duplicate products across suppliers
    deduplicate_products_in_db(db)

    # Seed default featured products if not yet configured
    if get_setting("featured_products", None) is None:
        init_ids = []
        try:
            p1 = db.execute("SELECT id FROM products WHERE name LIKE '%Gemini%18 Months%' AND active=1 ORDER BY id LIMIT 1").fetchone()
            if not p1:
                p1 = db.execute("SELECT id FROM products WHERE category='Gemini' AND active=1 ORDER BY price ASC LIMIT 1").fetchone() or db.execute("SELECT id FROM products WHERE active=1 ORDER BY price ASC LIMIT 1").fetchone()
            if p1:
                init_ids.append(str(p1["id"]))
            p2 = db.execute("SELECT id FROM products WHERE name LIKE '%Duolingo%' AND active=1 ORDER BY price ASC LIMIT 1").fetchone()
            if p2 and str(p2["id"]) not in init_ids:
                init_ids.append(str(p2["id"]))
            if init_ids:
                set_setting("featured_products", ",".join(init_ids))
        except Exception:
            pass


def resequence_product_ids(conn=None) -> Dict[int, int]:
    """Safely resequence products to consecutive IDs (1..N) and update referencing orders/stock."""
    target = conn or db
    try:
        cur = target.cursor()
        has_table = cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='products'").fetchone()[0]
        if not has_table:
            return {}
        prods = cur.execute("SELECT id, name, category FROM products ORDER BY category ASC, name ASC").fetchall()
        if not prods:
            return {}

        mapping = {p["id"]: idx for idx, p in enumerate(prods, 1)}
        if all(p["id"] == mapping[p["id"]] for p in prods):
            return mapping

        has_orders = cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='orders'").fetchone()[0]
        has_stock = cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='stock'").fetchone()[0]

        if has_orders:
            for old_id, new_id in mapping.items():
                cur.execute("UPDATE orders SET product_id = ? WHERE product_id = ?", (-new_id, old_id))
            cur.execute("UPDATE orders SET product_id = -product_id WHERE product_id < 0")

        if has_stock:
            for old_id, new_id in mapping.items():
                cur.execute("UPDATE stock SET product_id = ? WHERE product_id = ?", (-new_id, old_id))
            cur.execute("UPDATE stock SET product_id = -product_id WHERE product_id < 0")

        has_settings = cur.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='settings'").fetchone()[0]
        if has_settings:
            row_feat = cur.execute("SELECT value FROM settings WHERE key='featured_products'").fetchone()
            if row_feat and row_feat[0]:
                try:
                    raw_feat = str(row_feat[0]).strip()
                    if raw_feat.startswith("["):
                        f_list = json.loads(raw_feat)
                    else:
                        f_list = [int(x.strip()) for x in raw_feat.split(",") if x.strip().isdigit()]
                    if isinstance(f_list, list):
                        new_f_list = [mapping.get(pid, pid) for pid in f_list]
                        cur.execute("UPDATE settings SET value=? WHERE key='featured_products'", (",".join(str(x) for x in new_f_list),))
                except Exception:
                    pass

        # Clone table schema dynamically to preserve all columns across schema versions
        cur.execute("DROP TABLE IF EXISTS products_seq_tmp")
        schema_row = cur.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='products'").fetchone()
        if schema_row and schema_row[0]:
            create_sql = re.sub(r'\bCREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?["`]?products["`]?', 'CREATE TABLE products_seq_tmp', schema_row[0], count=1, flags=re.IGNORECASE)
            cur.execute(create_sql)
        else:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS products_seq_tmp (
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
                    delivery_type TEXT DEFAULT 'auto',
                    fixed_price INTEGER DEFAULT 0
                )
            """)

        for p in prods:
            old_id = p["id"]
            new_id = mapping[old_id]
            row = dict(cur.execute("SELECT * FROM products WHERE id = ?", (old_id,)).fetchone())
            row["id"] = new_id
            cols = list(row.keys())
            placeholders = ",".join("?" for _ in cols)
            cur.execute(f"INSERT INTO products_seq_tmp ({','.join(cols)}) VALUES ({placeholders})", [row[k] for k in cols])

        cur.execute("DROP TABLE products")
        cur.execute("ALTER TABLE products_seq_tmp RENAME TO products")
        cur.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'products'", (len(prods),))
        target.commit()
        log.info("Products resequenced successfully to 1..%d", len(prods))
        return mapping
    except Exception as e:
        log.warning("Product resequencing failed: %s", e)
        return {}


migrate_db()


async def refresh_catalog_excel(db_path: str = DB_PATH) -> Optional[str]:
    """Generate the catalog Excel file safely."""
    try:
        from generate_catalog_excel import build_catalog_excel
        return await build_catalog_excel(db_path)
    except Exception as e:
        log.warning("Catalog excel generation failed: %s", e)
        return None


def trigger_catalog_refresh(db_path: str = DB_PATH):
    """Dispatch background catalog Excel generation if not in testing."""
    if "unittest" in sys.modules:
        return None
    try:
        from generate_catalog_excel import build_catalog_excel
        return asyncio.create_task(build_catalog_excel(db_path))
    except Exception as e:
        log.warning("Catalog refresh dispatch failed: %s", e)
        return None


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
        "btn_check_pay": "🔄 Check Payment",
        "btn_manual_crypto": "💎 USDT / Binance Pay Details",
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
        "btn_check_pay": "🔄 Verificar Pago",
        "btn_manual_crypto": "💎 Detalles USDT / Binance Pay",
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
        "btn_check_pay": "🔄 Проверить оплату",
        "btn_manual_crypto": "💎 Реквизиты USDT / Binance Pay",
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
        "btn_check_pay": "🔄 التحقق من الدفع",
        "btn_manual_crypto": "💎 تفاصيل USDT / Binance Pay",
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


def get_featured_product_ids() -> List[int]:
    """Retrieve list of product IDs featured on the homepage."""
    raw = get_setting("featured_products", None)
    if raw is None:
        pids = []
        try:
            g = db.execute(
                "SELECT id FROM products WHERE name LIKE '%Gemini%18 Months%' AND active=1 ORDER BY id LIMIT 1"
            ).fetchone()
            if not g:
                g = db.execute(
                    "SELECT id FROM products WHERE category='Gemini' AND active=1 ORDER BY price ASC LIMIT 1"
                ).fetchone() or db.execute("SELECT id FROM products WHERE active=1 ORDER BY price ASC LIMIT 1").fetchone()
            if g:
                pids.append(g["id"])

            d = db.execute(
                "SELECT id FROM products WHERE name LIKE '%Duolingo%' AND active=1 ORDER BY price ASC LIMIT 1"
            ).fetchone()
            if not d and g:
                d = db.execute(
                    "SELECT id FROM products WHERE active=1 AND id != ? ORDER BY price ASC LIMIT 1", (g["id"],)
                ).fetchone()
            if d:
                pids.append(d["id"])
        except Exception:
            pass
        return pids

    if not raw.strip():
        return []

    pids = []
    cleaned = raw.strip().lstrip("[").rstrip("]")
    for part in cleaned.split(","):
        part = part.strip().strip('"').strip("'")
        if part.isdigit():
            pid = int(part)
            if pid not in pids:
                pids.append(pid)
    return pids


def get_featured_products(include_inactive: bool = False) -> List[sqlite3.Row]:
    """Return ordered list of featured product records."""
    pids = get_featured_product_ids()
    if not pids:
        return []
    try:
        placeholders = ",".join("?" for _ in pids)
        active_clause = "" if include_inactive else " AND active=1"
        rows = db.execute(f"SELECT * FROM products WHERE id IN ({placeholders}){active_clause}", pids).fetchall()
        row_map = {r["id"]: r for r in rows}
        return [row_map[pid] for pid in pids if pid in row_map]
    except Exception as e:
        log.warning("Failed to fetch featured products: %s", e)
        return []


def set_featured_product_ids(pids: List[int]):
    """Save ordered list of featured product IDs."""
    clean = []
    for pid in pids:
        if isinstance(pid, int) and pid not in clean:
            clean.append(pid)
    set_setting("featured_products", ",".join(str(i) for i in clean))


def add_featured_product_id(pid: int) -> bool:
    """Add a product ID to the featured list if not already present."""
    pids = get_featured_product_ids()
    if pid in pids:
        return False
    pids.append(pid)
    set_featured_product_ids(pids)
    return True


def remove_featured_product_id(pid: int) -> bool:
    """Remove a product ID from the featured list without touching catalogue."""
    pids = get_featured_product_ids()
    if pid not in pids:
        return False
    pids = [p for p in pids if p != pid]
    set_featured_product_ids(pids)
    return True


def render_featured_panel(admin_uid: int) -> Tuple[str, InlineKeyboardMarkup]:
    """Render the admin management view and keyboard for homepage featured products."""
    admin_curr = get_user_currency(admin_uid)
    all_featured = get_featured_products(include_inactive=True)
    pids = get_featured_product_ids()
    missing_ids = [pid for pid in pids if pid not in {p["id"]: True for p in all_featured}]

    text_lines = [
        "⭐ <b>Homepage Featured Products Management</b>\n",
        "Control products shown on the store home screen.\n",
    ]
    if not all_featured and not pids:
        text_lines.append("<i>No products currently featured. Homepage shows standard navigation buttons.</i>\n")
    else:
        text_lines.append("<b>Current Featured Products (in display order):</b>")
        for idx, p in enumerate(all_featured, 1):
            status = "🟢" if p["active"] else "🔴 [INACTIVE]"
            price_disp = format_price(p["price"], admin_curr)
            cat_disp = f" [{esc(p['category'])}]" if ("category" in p.keys() and p["category"]) else ""
            text_lines.append(f"{idx}. {status} <code>#{p['id']}</code> <b>{esc(p['name'])}</b>{cat_disp} · {price_disp}")

        for m_id in missing_ids:
            text_lines.append(f"⚠️ <code>#{m_id}</code> <i>(Deleted from catalogue)</i>")
        text_lines.append("")

    text_lines += [
        "<b>Commands:</b>",
        "• <code>/featured add &lt;id&gt;</code> — Add product to homepage",
        "• <code>/featured remove &lt;id&gt;</code> — Remove product from homepage",
        "• <code>/featured set &lt;id1,id2,...&gt;</code> — Replace all featured products",
        "• <code>/featured clear</code> — Remove all featured products"
    ]

    rows = []
    for p in all_featured:
        rows.append([btn(f"❌ Remove #{p['id']} {p['name'][:20]}", f"admin_f_del:{p['id']}", style="danger")])
    for m_id in missing_ids:
        rows.append([btn(f"❌ Remove #{m_id} (Deleted)", f"admin_f_del:{m_id}", style="danger")])

    action_row = [btn("➕ Add Product", "admin_f_add:1", style="success")]
    if all_featured or pids:
        action_row.append(btn("🗑 Clear All", "admin_f_clear", style="danger"))
    rows.append(action_row)

    rows.append([
        btn("👁 Preview Homepage", "home", style="primary"),
        btn("🔄 Refresh", "admin_featured", style="primary"),
        btn("⬅ Admin Menu", "admin_menu", style="primary"),
    ])
    return "\n".join(text_lines), kb(rows)


def render_featured_add_picker(admin_uid: int, page: int = 1) -> Tuple[str, InlineKeyboardMarkup]:
    """Render catalogue product picker to add to homepage featured."""
    PAGE_SIZE = 6
    prods = db.execute("SELECT * FROM products WHERE active=1 ORDER BY category ASC, name ASC").fetchall()
    total = len(prods)
    total_pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))
    start_idx = (page - 1) * PAGE_SIZE
    page_items = prods[start_idx:start_idx + PAGE_SIZE]

    current_featured = set(get_featured_product_ids())
    admin_curr = get_user_currency(admin_uid)

    text = (
        f"➕ <b>Select a Product to Feature on Homepage</b>\n\n"
        f"Showing page {page} of {total_pages} ({total} active products)\n"
        f"Tap any product button below to add it to the homepage."
    )

    rows = []
    for p in page_items:
        is_feat = p["id"] in current_featured
        mark = "⭐ " if is_feat else "➕ "
        price_disp = format_price(p["price"], admin_curr)
        name_trunc = p["name"][:20]
        btn_text = f"{mark}#{p['id']} {name_trunc} ({price_disp})"
        rows.append([btn(btn_text, f"admin_f_pick:{p['id']}:{page}", style="primary" if is_feat else "success")])

    nav_row = []
    if page > 1:
        nav_row.append(btn("⬅ Prev", f"admin_f_add:{page - 1}", style="primary"))
    nav_row.append(btn(f"📄 {page}/{total_pages}", "noop", style="primary"))
    if page < total_pages:
        nav_row.append(btn("Next ➡", f"admin_f_add:{page + 1}", style="primary"))
    rows.append(nav_row)

    rows.append([btn("⬅ Back to Featured Manager", "admin_featured", style="primary")])
    return text, kb(rows)


def home_kb(uid: int):
    """Home screen buttons."""
    rows = []

    featured_prods = get_featured_products()
    num_badges = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]

    for idx, p in enumerate(featured_prods):
        p_name_lower = p["name"].lower()
        emoji_id = None
        if "gemini" in p_name_lower:
            emoji_id = get_custom_emoji_id("gemini_product") or get_custom_emoji_id("gemini") or get_custom_emoji_id("google")
        elif "duolingo" in p_name_lower:
            emoji_id = get_custom_emoji_id("duolingo_product") or get_custom_emoji_id("duolingo") or get_custom_emoji_id("education")
        else:
            cat_key = p["category"].lower().replace(" ", "_") if ("category" in p.keys() and p["category"]) else ""
            emoji_id = get_custom_emoji_id(f"p_{p['id']}") or get_custom_emoji_id(cat_key)

        price_disp = format_user_price(p["price"], uid)
        if emoji_id:
            btn_text = f"{p['name']} · {price_disp}"
        else:
            if "duolingo" in p_name_lower:
                badge = "🦉"
            elif idx < len(num_badges):
                badge = num_badges[idx]
            else:
                badge = "⭐"
            btn_text = f"{badge} {p['name']} · {price_disp}"

        rows.append([btn(btn_text, f"p:{p['id']}:home", style="success", emoji_id=emoji_id)])

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
            stock_info = f" (📦 {sup_stock} pcs in stock)" if sup_stock is not None else ""
            stock_line = f"🟡 <b>Manual / Assisted Delivery</b>{stock_info}\n<i>⚡ Credentials or setup link delivered via support after payment</i>"
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
_order_locks: Dict[int, asyncio.Lock] = {}


def _get_order_lock(oid: int) -> asyncio.Lock:
    lock = _order_locks.get(oid)
    if lock is None:
        lock = asyncio.Lock()
        _order_locks[oid] = lock
    return lock


async def fulfill_order(bot: Bot, oid: int, force_supplier: bool = False, notify_manual: bool = True) -> Tuple[Optional[str], Optional[str]]:
    """
    Fulfills an order with an per-order async lock to prevent concurrent double-ordering:
      - Model A (when top-up balance > 0): Calls supplier API POST /orders immediately and auto-delivers.
      - Model B (when top-up balance is insufficient / empty): Pauses automated delivery and notifies admin to
        use customer's payment to top up, then tap /approve_<oid>.
      - If local stock: pulls available stock items from database.
    Returns (payload, error_key).
    """
    lock = _get_order_lock(oid)
    async with lock:
        try:
            return await _fulfill_order_locked(bot, oid, force_supplier, notify_manual)
        finally:
            if not lock.locked():
                _order_locks.pop(oid, None)


async def _fulfill_order_locked(bot: Bot, oid: int, force_supplier: bool = False, notify_manual: bool = True) -> Tuple[Optional[str], Optional[str]]:
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
                if notify_manual:
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
            rem = stock_count(p["id"])
            if not force_supplier and rem <= LOW_STOCK:
                await notify_admins(
                    bot,
                    f"⚠️ <b>Low Stock Alert!</b>\nProduct <b>{esc(p['name'])}</b> (#<code>{p['id']}</code>) only has <b>{rem}</b> left in stock!\n👉 Use <code>/addstock {p['id']} &lt;item&gt;</code> to restock."
                )
            return payload, None
        else:
            db.execute("ROLLBACK")
    except Exception:
        try:
            db.execute("ROLLBACK")
        except Exception:
            pass
        raise

    # 3. Manual delivery / retail fulfillment
    deliv = p["delivery_type"] if ("delivery_type" in p.keys() and p["delivery_type"]) else "auto"
    if deliv == "manual" or not p["auto_fulfill"]:
        db.execute("UPDATE orders SET status='manual_pending' WHERE id=?", (oid,))
        if notify_manual:
            buyer_uid = o["user_id"]
            user_mention = f"User <code>{buyer_uid}</code>"
            expected_note = f"\n• <b>Expected:</b> {esc(o['expected_amount'])}" if ("expected_amount" in o.keys() and o["expected_amount"]) else ""
            method_str = o["payment_method"] if ("payment_method" in o.keys() and o["payment_method"]) else "Manual"
            txid_val = o["txid"] if ("txid" in o.keys() and o["txid"]) else (o["invoice_id"] if "invoice_id" in o.keys() else "")
            tx_line = f"\n• <b>TxID / Sender:</b> <code>{esc(txid_val)}</code>" if txid_val else ""

            await notify_admins(
                bot,
                lambda aid: (
                    f"🔔 <b>New Order Payment Verified!</b>\n"
                    f"• <b>Order:</b> #{oid}\n"
                    f"• <b>Buyer:</b> {user_mention}\n"
                    f"• <b>Product:</b> {esc(p['name'])}\n"
                    f"• <b>Amount:</b> {format_user_price(o['price'], aid)}{expected_note}\n"
                    f"• <b>Method:</b> {esc(str(method_str).upper())}{tx_line}\n\n"
                    f"✅ Approve & deliver: /approve_{oid}\n"
                    f"❌ Reject: /reject_{oid}"
                )
            )
        return "MANUAL_PENDING", None

    db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
    return None, "out_of_stock"


# User handlers
@dp.message(CommandStart())
@dp.message(Command("start"))
@dp.message(Command("menu"))
async def start(m: Message, command: CommandObject = None):
    pending_inputs.pop(m.from_user.id, None)
    uid = m.from_user.id
    get_user(uid, m.from_user.language_code)
    passed, missing = await check_user_fsub(m.bot, uid)
    if not passed:
        text, markup = render_fsub_gate(missing)
        return await m.answer(text, reply_markup=markup)

    args = (command.args or "").strip() if command and command.args else ""
    if not args and isinstance(m.text, str) and len(m.text.split()) > 1:
        args = m.text.split(maxsplit=1)[1].strip()

    if args.startswith("order_"):
        oid_str = args[6:].strip()
        if oid_str.isdigit():
            oid = int(oid_str)
            o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if o and (o["user_id"] == uid or uid in ADMIN_IDS):
                if o["status"] == "pending" and o["payment_method"] == "bachs" and o["invoice_id"]:
                    is_paid, _ = await verify_bachs_transaction(o["invoice_id"])
                    if is_paid:
                        db.execute("UPDATE orders SET status='paid' WHERE id=?", (oid,))
                        payload, _ = await fulfill_order(m.bot, oid)
                        if payload and payload != "MANUAL_PENDING":
                            return await m.answer(t(uid, "purchased", oid=oid, payload=esc(payload)))
                if o["status"] == "completed" and o["payload"]:
                    return await m.answer(t(uid, "purchased", oid=oid, payload=esc(o["payload"])))
                p_disp = format_user_price(o["price"], uid)
                status_icon = "🟢" if o["status"] == "completed" else ("🟡" if o["status"] in ("paid", "manual_pending") else "⏳")
                return await m.answer(
                    f"🧾 <b>Order #{oid} Details</b>\n\n"
                    f"• <b>Item:</b> {esc(o['product_name'])}\n"
                    f"• <b>Total:</b> {p_disp}\n"
                    f"• <b>Status:</b> {status_icon} <i>{o['status'].capitalize()}</i>\n\n"
                    f"<i>You can view all your purchases anytime with /orders</i>",
                    reply_markup=kb([[btn(t(uid, "btn_back"), "orders", style="primary")]])
                )
    elif args.startswith("p_"):
        pid_str = args[2:].strip()
        if pid_str.isdigit():
            pid = int(pid_str)
            p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
            if p:
                text, markup = render_product_card(uid, p, "start")
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


async def initialize_bachs_transaction(
    order_id: int,
    amount_cents: int,
    product_name: str,
    customer_email: Optional[str] = None,
    uid: Optional[int] = None,
    qty: int = 1,
    success_url_override: Optional[str] = None,
    cancel_url_override: Optional[str] = None
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
        or "creditlogbot"
    ).strip().lstrip("@")

    tg_url = f"https://t.me/{bot_username}?start=order_{order_id}"
    success_url = success_url_override or callback_url or tg_url
    cancel_url = cancel_url_override or callback_url or tg_url

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
    binance_emoji = get_custom_emoji_id("binance") or get_custom_emoji_id("binance_pay")
    usdt_emoji = get_custom_emoji_id("usdt") or get_custom_emoji_id("tether")
    card_emoji = get_custom_emoji_id("card") or get_custom_emoji_id("fiat")
    back_emoji = get_custom_emoji_id("back")

    binance_label = "Binance Pay (USDT only)" if binance_emoji else "🟡 Binance Pay (USDT only)"
    usdt_label = "USDT (TRC20 / BEP20)" if usdt_emoji else "🟢 USDT (TRC20 / BEP20)"
    card_label = "Don't have crypto?" if card_emoji else "💳 Don't have crypto?"
    back_label = "Back" if back_emoji else "↩️ Back"

    rows = [
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



def check_product_stock_available(p: sqlite3.Row, qty: int) -> Tuple[bool, int]:
    """Check if product has enough manual stock for requested quantity."""
    is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
    deliv_type = p["delivery_type"] if ("delivery_type" in p.keys() and p["delivery_type"]) else ("auto" if is_auto else "manual")
    if not is_auto and deliv_type != "manual":
        try:
            has_stock_rows = db.execute("SELECT 1 FROM stock WHERE product_id=?", (p["id"],)).fetchone()
            if has_stock_rows:
                avail = stock_count(p["id"])
                if avail < qty:
                    return False, avail
        except Exception:
            pass
    return True, 0


def parse_cart_callback(c: CallbackQuery) -> Tuple[Optional[Tuple[int, int, str, sqlite3.Row]], Optional[str]]:
    """Validate and parse cart callback in format 'prefix:pid:qty:ctx'."""
    parts = c.data.split(":", 3)
    if len(parts) < 4 or not parts[1].isdigit() or not parts[2].isdigit():
        return None, "⚠️ Invalid callback data."
    _, pid_str, qty_str, ctx = parts
    pid, qty = int(pid_str), int(qty_str)
    if not (1 <= qty <= 1000):
        return None, "⚠️ Invalid quantity."
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return None, t(c.from_user.id, "out_of_stock")
    return (pid, qty, ctx, p), None


def build_order_action_kb(oid: int, action_btn: InlineKeyboardButton, check_callback: str, back_callback: str) -> InlineKeyboardMarkup:
    """Standard 4-row keyboard for payment screens."""
    return kb([
        [action_btn],
        [btn("✅ I've Paid — Check Status", check_callback, style="success")],
        [btn("↩️ Back", back_callback, style="danger")],
        [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
    ])


async def start_checkout(event: Message | CallbackQuery, uid: int, p: sqlite3.Row, qty: int, ctx: str):
    """Start checkout summary."""
    if not (1 <= qty <= 1000):
        qty = 1
    ok, avail = check_product_stock_available(p, qty)
    if not ok:
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
    res, err = parse_cart_callback(c)
    if err:
        return await c.answer(err, show_alert=True)
    pid, qty, ctx, p = res
    text, markup = render_order_summary(c.from_user.id, p, qty, ctx)
    await edit(c, text, markup)


@dp.callback_query(F.data.startswith("pay_dir:"))
async def on_pay_directly(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    res, err = parse_cart_callback(c)
    if err:
        return await c.answer(err, show_alert=True)
    pid, qty, ctx, p = res
    text, markup = render_payment_methods(c.from_user.id, p, qty, ctx)
    await edit(c, text, markup)


@dp.callback_query(F.data.startswith("pay_usdt_net:"))
async def on_pay_usdt_network(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    res, err = parse_cart_callback(c)
    if err:
        return await c.answer(err, show_alert=True)
    pid, qty, ctx, p = res

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
    res, err = parse_cart_callback(c)
    if err:
        return await c.answer(err, show_alert=True)
    pid, qty, ctx, p = res

    if not is_bank_configured() and not is_bachs_configured():
        return await c.answer("⚠️ Local / fiat payment methods are currently being set up. Please select Binance Pay or USDT.", show_alert=True)

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
    if method not in ("bachs", "bank", "binance", "trc20", "bep20"):
        return await c.answer("⚠️ This payment method is no longer available. Please choose another payment method.", show_alert=True)
    pid, qty = int(pid_str), int(qty_str)
    if not (1 <= qty <= 1000):
        return await c.answer("⚠️ Invalid quantity.", show_alert=True)
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)

    ok, avail = check_product_stock_available(p, qty)
    if not ok:
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
        return await edit(c, text, build_order_action_kb(
            oid,
            btn("💳 Pay with Bachs", url=checkout_url),
            f"chk_bachs:{oid}:{checkout_id}",
            f"pay_fiat:{pid}:{qty}:{ctx}"
        ))

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
        return await edit(c, text, build_order_action_kb(
            oid,
            btn("🔗 Submit Transfer Reference / Name", f"submit_tx:{oid}", style="primary"),
            f"chk_manual:{oid}",
            f"pay_fiat:{pid}:{qty}:{ctx}"
        ))

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
        return await edit(c, text, build_order_action_kb(
            oid,
            btn("🔗 Submit Transaction ID", f"submit_tx:{oid}", style="primary"),
            f"chk_manual:{oid}",
            f"pay_dir:{pid}:{qty}:{ctx}"
        ))

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
        return await edit(c, text, build_order_action_kb(
            oid,
            btn("🔗 Submit Transaction ID", f"submit_tx:{oid}", style="primary"),
            f"chk_manual:{oid}",
            f"pay_usdt_net:{pid}:{qty}:{ctx}"
        ))

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
        return await edit(c, text, build_order_action_kb(
            oid,
            btn("🔗 Submit Transaction ID", f"submit_tx:{oid}", style="primary"),
            f"chk_manual:{oid}",
            f"pay_usdt_net:{pid}:{qty}:{ctx}"
        ))


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
        payload, err = await fulfill_order(bot_instance, oid, notify_manual=False)
        if err:
            log.error("Failed to fulfill auto-verified order %s: %s", oid, err)
            await notify_admins(
                bot_instance,
                f"⚠️ Payment verified on-chain for Order #{oid}, but auto-fulfillment failed: {esc(str(err))}\n"
                f"• Method: {esc(method.upper())}\n"
                f"• TxID: <code>{esc(txid)}</code>\n"
                f"👉 Use <code>/approve_{oid}</code> to retry or <code>/deliver {oid} &lt;item&gt;</code> to deliver manually."
            )
            return True, "Payment verified on-chain! Our support team is completing your delivery shortly."

        buyer_username = ""
        try:
            chat_obj = await bot_instance.get_chat(uid)
            if chat_obj and chat_obj.username:
                buyer_username = f"@{chat_obj.username}"
        except Exception:
            pass
        user_mention = f"{buyer_username} (<code>{uid}</code>)" if buyer_username else f"User <code>{uid}</code>"

        if payload == "MANUAL_PENDING":
            await notify_admins(
                bot_instance,
                lambda aid: (
                    f"🔔 <b>New Order Payment Verified!</b>\n"
                    f"• <b>Order:</b> #{oid}\n"
                    f"• <b>Buyer:</b> {user_mention}\n"
                    f"• <b>Product:</b> {esc(o['product_name'])}\n"
                    f"• <b>Amount:</b> ${detected_amount:.2f} USDT\n"
                    f"• <b>Expected:</b> {usd(o['price'])}\n"
                    f"• <b>Method:</b> {esc(method.upper())}\n"
                    f"• <b>TxID / Sender:</b> <code>{esc(txid)}</code>\n\n"
                    f"✅ Approve & deliver: /approve_{oid}\n"
                    f"❌ Reject: /reject_{oid}"
                )
            )
            return True, "MANUAL_PENDING"

        await notify_admins(
            bot_instance,
            lambda aid: (
                f"⚡ <b>Auto-Verified & Delivered!</b>\n"
                f"• <b>Order:</b> #{oid}\n"
                f"• <b>Buyer:</b> {user_mention}\n"
                f"• <b>Product:</b> {esc(o['product_name'])}\n"
                f"• <b>Amount:</b> ${detected_amount:.2f} USDT (Required: {usd(o['price'])})\n"
                f"• <b>Method:</b> {esc(method.upper())}\n"
                f"• <b>TxID:</b> <code>{esc(txid)}</code>"
            )
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
    res, err = parse_cart_callback(c)
    if err:
        return await c.answer(err, show_alert=True)
    pid, qty, ctx, p = res
    if not is_product_available(p):
        return await c.answer(t(c.from_user.id, "out_of_stock"), show_alert=True)
    sup_stock = p["supplier_stock"] if "supplier_stock" in p.keys() else None
    if sup_stock is not None and sup_stock >= 0 and qty > sup_stock:
        return await c.answer(f"⚠️ Only {sup_stock} unit(s) available in stock.", show_alert=True)
    await start_checkout(c, c.from_user.id, p, qty, ctx)


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
        sup_stock = p["supplier_stock"] if "supplier_stock" in p.keys() else None
        if sup_stock is not None and sup_stock >= 0 and qty > sup_stock:
            pending_inputs[uid] = state
            return await m.answer(f"⚠️ Only {sup_stock} in stock at supplier. Please enter a quantity up to {sup_stock} (or send /cancel):")

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
        txid = m.text.strip().strip('"').strip("'")
        url_m = re.search(r"(?:tx|transaction)\/(?:0x)?([A-Fa-f0-9]{64})", txid, re.IGNORECASE)
        if url_m:
            txid = url_m.group(1)
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


@dp.message(F.text.regexp(r"^/approve_(\d+)(?:@\w+)?(?:\s+(.+))?$"), is_admin)
@dp.message(Command("approve"), is_admin)
async def admin_approve_order(m: Message, command: CommandObject = None):
    oid = None
    direct_payload = None
    if command and command.args:
        parts = command.args.strip().split(maxsplit=1)
        if parts:
            try:
                oid = int(parts[0])
                if len(parts) > 1:
                    direct_payload = parts[1].strip()
            except ValueError:
                pass
    elif m.text and m.text.startswith("/approve_"):
        raw_cmd = m.text[9:]  # after "/approve_"
        parts = raw_cmd.split(maxsplit=1)
        first = parts[0].split("@")[0]
        try:
            oid = int(first)
            if len(parts) > 1:
                direct_payload = parts[1].strip()
        except (ValueError, IndexError):
            pass

    if not oid:
        return await m.answer("Usage: /approve <order_id> [credentials] or /approve_<order_id> [credentials]")

    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return await m.answer(f"Order #{oid} not found.")
    if o["status"] == "completed":
        return await m.answer(f"Order #{oid} is already completed.")
    if o["refunded"] or o["status"] in ("refunded", "cancelled", "rejected"):
        return await m.answer(f"⚠️ Order #{oid} was refunded or cancelled.")

    buyer_uid = o["user_id"]

    # If admin explicitly provided credentials with /approve, deliver directly
    if direct_payload:
        db.execute("UPDATE orders SET payload=?, status='completed' WHERE id=?", (direct_payload, oid))
        try:
            await m.bot.send_message(buyer_uid, t(buyer_uid, "purchased", oid=oid, payload=esc(direct_payload)))
            return await m.answer(f"✅ Order #{oid} approved and delivered to user {buyer_uid}!")
        except Exception as e:
            return await m.answer(f"✅ Order #{oid} completed, but could not message user: {esc(str(e))}\nPayload: <code>{esc(direct_payload)}</code>")

    payload, err = await fulfill_order(m.bot, oid, force_supplier=True, notify_manual=False)
    if err:
        return await m.answer(f"⚠️ Failed to fulfill order #{oid}: {esc(str(err))}")

    try:
        if payload == "MANUAL_PENDING":
            await m.bot.send_message(
                buyer_uid,
                f"🎉 <b>Payment Confirmed for Order #{oid}!</b>\n\n"
                f"Item: <b>{esc(o['product_name'])}</b>\n\n"
                f"🟡 <i>This product is an assisted/manual setup service. Our support team has received your order and will deliver your credentials or setup link directly to you here shortly!</i>\n\n"
                f"If you have any questions, contact Support: {SUPPORT_URL}"
            )
            await m.answer(f"✅ Order #{oid} approved. Send credentials to deliver: <code>/deliver {oid} &lt;item&gt;</code>")
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


def render_admin_help() -> Tuple[str, InlineKeyboardMarkup]:
    text = (
        "<b>Admin Commands</b>\n\n"
        "/start - Start bot / Open store\n"
        "/admin - Admin command center\n\n"
        "<b>Catalog & Products</b>\n"
        "/featured - Manage homepage featured products (add/remove/set/clear)\n"
        "/products - List all products with margins\n"
        "/addproduct &lt;price&gt; [currency] &lt;name&gt; [| description] (e.g. $12.50 or 15000 NGN)\n"
        "/setprice &lt;product_id&gt; &lt;price&gt; [currency] (e.g. /setprice 1 15000 NGN or $12.50)\n"
        "/clearprices [product_id] - Clear manual price overrides & reset to dynamic supplier pricing\n"
        "/clearcache - Clear all cached products & sync totally from supplier API\n"
        "/sync_retail - Sync retail catalog descriptions and stock from supplier scraper data\n"
        "/crawl_retail - Crawl live supplier bot to discover new SKUs and refresh stock\n"
        "/setmargin &lt;product_id&gt; &lt;margin_%&gt; (e.g. /setmargin 1 60%)\n"
        "/toggle &lt;product_id&gt;  (activate/deactivate)\n"
        "/addstock &lt;product_id&gt; &lt;item&gt;  (manual items, 1 per line)\n"
        "/refund &lt;order_id&gt;\n"
        "/stats - View sales stats\n"
        "/broadcast &lt;text&gt; - Broadcast message to buyers\n"
        "/broadcast [play|pause] - Play or pause automated stock broadcaster\n"
        "/broadcastplay / /broadcastpause - Play/resume or pause automated restock broadcasts\n"
        "/backup - Download fresh database backup (.db)\n"
        "/export - Download updated products Excel (.xlsx)\n"
        "/setrate [rate_ngn] - View or update USD to NGN exchange rate\n"
        "/currency - Switch your active display currency (USD / NGN)\n"
        "/setbachs [api_key] - Configure Bachs Payments Gateway\n"
        "/bachs [play|pause] - View Bachs Gateway status, play or pause checkout\n"
        "/bachsplay / /bachspause - Play (resume) or pause Bachs payments\n"
        "/bachscurrency [USD|NGN] - Set Bachs checkout currency\n"
        "/setbank [Bank | AccNum | Name] - Configure or update Direct Bank Transfer\n"
        "/addbank &lt;Bank | AccNum | Name&gt; - Add another bank account (multi-bank)\n"
        "/delbank [number|all] - Remove a bank account or disable bank transfer\n"
        "/bank - View current Bank Transfer configuration\n\n"
        "🎨 <b>Brand Custom Emojis</b>\n"
        "/setemoji &lt;key&gt; [emoji] - Set official brand custom emoji ID\n"
        "/emojis - View configured brand custom emojis\n"
        "/delemoji &lt;key&gt; - Remove custom emoji\n\n"
        "📂 <b>Category Management</b>\n"
        "/categories - View all store categories & item counts\n"
        "/addcategory &lt;name&gt; [icon] - Add or update a category\n"
        "/delcategory &lt;key_or_name&gt; - Remove a category\n\n"
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
    markup = kb([
        [btn("⭐ Homepage Products", "admin_featured", style="primary"),
         btn("📊 Export Excel", "admin_export_excel", style="success")]
    ])
    return text, markup


# Admin commands
@dp.message(Command("admin"), is_admin)
async def admin_help(m: Message):
    text, markup = render_admin_help()
    await m.answer(text, reply_markup=markup)


@dp.callback_query(F.data == "admin_menu")
async def cb_admin_menu(c: CallbackQuery):
    if not (c.from_user and c.from_user.id in ADMIN_IDS):
        return await c.answer("Unauthorized", show_alert=True)
    text, markup = render_admin_help()
    await edit(c, text, markup)
    await c.answer()


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
        formatted_channels = ", ".join(f"<code>{esc(c)}</code>" for c in new_channels)
        return await m.answer(f"✅ Configured {len(new_channels)} required channels: {formatted_channels}")
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
            f"<code>trc20, bep20, binance, usdt, card, back, bank</code>\n\n"
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


@dp.message(Command("addcategory"), is_admin)
async def admin_add_category(m: Message, command: CommandObject = None):
    args = (command.args or "").strip() if command else ""
    if not args:
        return await m.answer(
            "<b>Usage:</b> <code>/addcategory &lt;name&gt; [icon]</code>\n"
            "<b>Or:</b> <code>/addcategory &lt;key&gt; &lt;name&gt; &lt;icon&gt;</code>\n\n"
            "<b>Examples:</b>\n"
            "• <code>/addcategory Midjourney 🎨</code>\n"
            "• <code>/addcategory prompt_base PromptBase 💡</code>"
        )

    parts = args.split()
    if len(parts) >= 3 and re.match(r'^[a-z0-9_]+$', parts[0]):
        key = parts[0].lower()
        icon = parts[-1]
        name = " ".join(parts[1:-1])
    elif len(parts) >= 2:
        last = parts[-1]
        if len(last) <= 4 and not last.isalnum():
            icon = last
            name = " ".join(parts[:-1])
        else:
            name = " ".join(parts)
            icon = "📦"
        key = re.sub(r'[^a-z0-9_]', '', name.lower().replace(" ", "_"))
    else:
        name = parts[0]
        key = re.sub(r'[^a-z0-9_]', '', name.lower().replace(" ", "_"))
        icon = "📦"

    if not key:
        return await m.answer("⚠️ Could not generate a valid key for this category.")

    try:
        max_order_row = db.execute("SELECT MAX(sort_order) FROM categories").fetchone()
        next_order = (max_order_row[0] + 1) if max_order_row and max_order_row[0] is not None else len(CATEGORIES)
        db.execute("""
            INSERT INTO categories (key, name, icon, sort_order)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET name=excluded.name, icon=excluded.icon
        """, (key, name, icon, next_order))
        db.commit()
        reload_categories()
        await m.answer(
            f"✅ <b>Category added / updated:</b>\n\n"
            f"• <b>Name:</b> {esc(name)}\n"
            f"• <b>Key:</b> <code>{esc(key)}</code>\n"
            f"• <b>Icon:</b> {icon}\n\n"
            f"<i>Total active categories: {len(CATEGORIES)}</i>"
        )
    except Exception as e:
        await m.answer(f"❌ Failed to save category: {esc(str(e))}")


@dp.message(Command("delcategory", "removecategory", "deletecategory"), is_admin)
async def admin_del_category(m: Message, command: CommandObject = None):
    args = (command.args or "").strip() if command else ""
    if not args:
        return await m.answer(
            "<b>Usage:</b> <code>/delcategory &lt;key_or_name&gt;</code>\n\n"
            "<b>Example:</b>\n"
            "• <code>/delcategory midjourney</code>\n"
            "• <code>/delcategory Midjourney</code>"
        )

    target = args.lower()
    matched_key = None
    matched_name = None
    for k, n, ic in CATEGORIES:
        if k.lower() == target or n.lower() == target:
            matched_key = k
            matched_name = n
            break

    if not matched_key:
        return await m.answer(f"⚠️ Category matching <b>{esc(args)}</b> not found.")

    try:
        db.execute("DELETE FROM categories WHERE key = ?", (matched_key,))
        db.commit()
        reload_categories()
        await m.answer(
            f"🗑️ <b>Category removed:</b> {esc(matched_name)} (<code>{esc(matched_key)}</code>)\n\n"
            f"<i>Remaining categories: {len(CATEGORIES)}</i>"
        )
    except Exception as e:
        await m.answer(f"❌ Failed to remove category: {esc(str(e))}")


@dp.message(Command("categories", "listcategories"), is_admin)
async def admin_list_categories(m: Message):
    reload_categories()
    lines = [
        f"📂 <b>Store Categories ({len(CATEGORIES)} Total)</b>:\n",
    ]
    for idx, (k, n, ic) in enumerate(CATEGORIES, start=1):
        prods = get_products_for_category(k)
        lines.append(f"{idx}. {ic} <b>{esc(n)}</b> (<code>{esc(k)}</code>) — {len(prods)} products")

    lines.append(
        "\n💡 <i>Commands:</i>\n"
        "• <code>/addcategory &lt;name&gt; [icon]</code> to add or update a category\n"
        "• <code>/delcategory &lt;key_or_name&gt;</code> to remove a category"
    )
    await m.answer("\n".join(lines))


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


@dp.message(Command("featured"), is_admin)
@dp.message(Command("homepage"), is_admin)
async def admin_featured_cmd(m: Message, command: CommandObject = None):
    args = (command.args or "").strip() if command else ""
    if not args and isinstance(m.text, str) and len(m.text.split()) > 1:
        args = m.text.split(maxsplit=1)[1].strip()

    if not args:
        text, markup = render_featured_panel(m.from_user.id)
        return await m.answer(text, reply_markup=markup)

    parts = args.split(maxsplit=1)
    action = parts[0].lower()
    sub_args = parts[1].strip() if len(parts) > 1 else ""

    if action in ("add", "+"):
        if not sub_args:
            return await m.answer("Usage: <code>/featured add &lt;product_id&gt;</code> (e.g. <code>/featured add 85</code> or <code>/featured add 85, 72</code>)")
        raw_tokens = [tok for tok in re.split(r"[,\s]+", sub_args) if tok]
        added = []
        already = []
        missing = []
        for tok in raw_tokens:
            if tok.isdigit():
                pid = int(tok)
                p = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
                if not p:
                    missing.append(str(pid))
                elif add_featured_product_id(pid):
                    added.append(f"#{pid} {p['name']}")
                else:
                    already.append(f"#{pid}")
            else:
                missing.append(tok)

        reply_parts = []
        if added:
            reply_parts.append(f"✅ Added {len(added)} product(s) to homepage: <b>{esc(', '.join(added))}</b>")
        if already:
            reply_parts.append(f"⚠️ Already featured: <code>{', '.join(esc(x) for x in already)}</code>")
        if missing:
            reply_parts.append(f"❌ Not found in catalogue: <code>{', '.join(esc(x) for x in missing)}</code>")

        return await m.answer(
            "\n".join(reply_parts) if reply_parts else "❌ No valid product IDs provided.",
            reply_markup=kb([[btn("⭐ Manage Featured", "admin_featured", style="primary"), btn("👁 View Homepage", "home", style="success")]])
        )

    elif action in ("remove", "del", "rm", "-"):
        if not sub_args:
            return await m.answer("Usage: <code>/featured remove &lt;product_id&gt;</code> (e.g. <code>/featured remove 85</code>)")
        raw_tokens = [tok for tok in re.split(r"[,\s]+", sub_args) if tok]
        removed = []
        not_feat = []
        for tok in raw_tokens:
            if tok.isdigit():
                pid = int(tok)
                p = db.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
                name_str = f"#{pid} {p['name']}" if p else f"#{pid}"
                if remove_featured_product_id(pid):
                    removed.append(name_str)
                else:
                    not_feat.append(str(pid))
            else:
                not_feat.append(tok)

        reply_parts = []
        if removed:
            reply_parts.append(f"✅ Removed {len(removed)} product(s) from homepage: <b>{esc(', '.join(removed))}</b>\n<i>(Note: Products remain active in the store catalogue)</i>")
        if not_feat:
            reply_parts.append(f"⚠️ Not currently featured: <code>{', '.join(esc(x) for x in not_feat)}</code>")

        return await m.answer(
            "\n".join(reply_parts) if reply_parts else "❌ No valid product IDs provided.",
            reply_markup=kb([[btn("⭐ Manage Featured", "admin_featured", style="primary")]])
        )

    elif action == "clear":
        set_featured_product_ids([])
        return await m.answer(
            "✅ Cleared all featured products from the homepage.\n<i>The homepage will now display standard navigation buttons.</i>",
            reply_markup=kb([[btn("⭐ Manage Featured", "admin_featured", style="primary"), btn("👁 View Homepage", "home", style="success")]])
        )

    elif action == "set":
        if not sub_args:
            return await m.answer("Usage: <code>/featured set &lt;id1,id2,...&gt;</code> (e.g. <code>/featured set 85, 72</code>)")
        raw_tokens = re.split(r"[,\s]+", sub_args)
        valid_ids = []
        invalid_ids = []
        for tok in raw_tokens:
            if tok.isdigit():
                pid = int(tok)
                p = db.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone()
                if p:
                    if pid not in valid_ids:
                        valid_ids.append(pid)
                else:
                    invalid_ids.append(tok)
            elif tok:
                invalid_ids.append(tok)

        if not valid_ids:
            return await m.answer("❌ No valid product IDs provided.")

        set_featured_product_ids(valid_ids)
        warning = f"\n⚠️ Ignored unknown IDs: {', '.join(esc(i) for i in invalid_ids)}" if invalid_ids else ""
        return await m.answer(
            f"✅ Homepage featured products updated ({len(valid_ids)} items): <code>{', '.join(str(i) for i in valid_ids)}</code>{warning}",
            reply_markup=kb([[btn("⭐ Manage Featured", "admin_featured", style="primary"), btn("👁 View Homepage", "home", style="success")]])
        )

    else:
        raw_tokens = re.split(r"[,\s]+", args)
        if all(tok.isdigit() for tok in raw_tokens if tok):
            valid_ids = []
            for tok in raw_tokens:
                if tok.isdigit():
                    pid = int(tok)
                    if db.execute("SELECT id FROM products WHERE id=?", (pid,)).fetchone() and pid not in valid_ids:
                        valid_ids.append(pid)
            if valid_ids:
                set_featured_product_ids(valid_ids)
                return await m.answer(
                    f"✅ Homepage featured products set to: <code>{', '.join(str(i) for i in valid_ids)}</code>",
                    reply_markup=kb([[btn("⭐ Manage Featured", "admin_featured", style="primary"), btn("👁 View Homepage", "home", style="success")]])
                )

        text, markup = render_featured_panel(m.from_user.id)
        return await m.answer(text, reply_markup=markup)


@dp.callback_query(F.data == "admin_featured")
async def cb_admin_featured(c: CallbackQuery):
    if not (c.from_user and c.from_user.id in ADMIN_IDS):
        return await c.answer("Unauthorized", show_alert=True)
    text, markup = render_featured_panel(c.from_user.id)
    await edit(c, text, markup)
    await c.answer()


@dp.callback_query(F.data.startswith("admin_f_del:"))
async def cb_admin_featured_del(c: CallbackQuery):
    if not (c.from_user and c.from_user.id in ADMIN_IDS):
        return await c.answer("Unauthorized", show_alert=True)
    pid_str = c.data.split(":")[1]
    if pid_str.isdigit():
        pid = int(pid_str)
        p = db.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
        pname = p["name"] if p else f"#{pid}"
        if remove_featured_product_id(pid):
            await c.answer(f"Removed #{pid} from homepage.", show_alert=False)
        else:
            await c.answer("Product was not featured.", show_alert=False)
    text, markup = render_featured_panel(c.from_user.id)
    await edit(c, text, markup)


@dp.callback_query(F.data == "admin_f_clear")
async def cb_admin_featured_clear(c: CallbackQuery):
    if not (c.from_user and c.from_user.id in ADMIN_IDS):
        return await c.answer("Unauthorized", show_alert=True)
    set_featured_product_ids([])
    await c.answer("All featured products cleared.", show_alert=False)
    text, markup = render_featured_panel(c.from_user.id)
    await edit(c, text, markup)


@dp.callback_query(F.data.startswith("admin_f_add:"))
async def cb_admin_featured_add_page(c: CallbackQuery):
    if not (c.from_user and c.from_user.id in ADMIN_IDS):
        return await c.answer("Unauthorized", show_alert=True)
    page_str = c.data.split(":")[1]
    page = int(page_str) if page_str.isdigit() else 1
    text, markup = render_featured_add_picker(c.from_user.id, page)
    await edit(c, text, markup)
    await c.answer()


@dp.callback_query(F.data.startswith("admin_f_pick:"))
async def cb_admin_featured_pick(c: CallbackQuery):
    if not (c.from_user and c.from_user.id in ADMIN_IDS):
        return await c.answer("Unauthorized", show_alert=True)
    parts = c.data.split(":")
    pid_str = parts[1]
    page_str = parts[2] if len(parts) > 2 else "1"
    page = int(page_str) if page_str.isdigit() else 1
    if pid_str.isdigit():
        pid = int(pid_str)
        p = db.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()
        pname = p["name"] if p else f"#{pid}"
        if add_featured_product_id(pid):
            await c.answer(f"Added #{pid} {pname[:20]} to homepage!", show_alert=False)
        else:
            remove_featured_product_id(pid)
            await c.answer(f"Removed #{pid} from homepage!", show_alert=False)
    text, markup = render_featured_add_picker(c.from_user.id, page)
    await edit(c, text, markup)


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

        cat_disp = f" [{esc(p['category'])}]" if ("category" in p.keys() and p["category"]) else ""
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
            trigger_catalog_refresh()
            await m.answer(
                f"✅ Price for #{pid} (<b>{esc(p['name'])}</b>) updated to {disp_price}.\n"
                f"• Wholesale Cost: <b>{format_price(cost, admin_curr)}</b>\n"
                f"• Adjusted Margin: <b>{calc_margin}%</b> (fixed price locked: future supplier cost changes will adjust the margin, keeping this selling price unchanged)"
            )
        else:
            db.execute("UPDATE products SET price=?, fixed_price=1 WHERE id=?", (new_price_cents, pid))
            trigger_catalog_refresh()
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
        trigger_catalog_refresh()

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


@dp.message(Command("clearprices", "resetprices", "clearprice"), is_admin)
async def admin_clearprices(m: Message, command: CommandObject = None):
    """Clear manual fixed prices and reset products back to dynamic supplier margin."""
    admin_curr = get_user_currency(m.from_user.id)
    global_margin = get_default_margin()

    # Reset a single product if an ID was supplied
    if command and command.args and command.args.strip().isdigit():
        pid = int(command.args.strip())
        p = db.execute("SELECT * FROM products WHERE id=?", (pid,)).fetchone()
        if not p:
            return await m.answer(f"⚠️ Product #{pid} not found.")

        cost = p["supplier_price"]
        if cost and cost > 0:
            new_price = max(100, round(cost * (1 + global_margin / 100)))
            db.execute("UPDATE products SET fixed_price=0, margin=NULL, price=? WHERE id=?", (new_price, pid))
            price_msg = f"recalculated to <b>{format_price(new_price, admin_curr)}</b> (wholesale {format_price(cost, admin_curr)} + {global_margin}% margin)"
        else:
            db.execute("UPDATE products SET fixed_price=0, margin=NULL WHERE id=?", (pid,))
            price_msg = "fixed price lock removed"

        db.commit()
        trigger_catalog_refresh()
        return await m.answer(f"✅ Reset Product #{pid} (<b>{esc(p['name'])}</b>): {price_msg}.")

    # Wipe all fixed price locks and custom margins across the store
    overridden = db.execute(
        "SELECT id, name, price, supplier_price, fixed_price, margin FROM products WHERE fixed_price=1 OR margin IS NOT NULL"
    ).fetchall()

    db.execute("UPDATE products SET fixed_price=0, margin=NULL")

    # Recalculate price for all supplier products using default store margin
    supplier_prods = db.execute(
        "SELECT id, supplier_price FROM products WHERE supplier_product_id IS NOT NULL AND supplier_price > 0"
    ).fetchall()

    recalc_count = 0
    for sp in supplier_prods:
        cost = sp["supplier_price"]
        recalculated_price = max(100, round(cost * (1 + global_margin / 100)))
        db.execute("UPDATE products SET price=? WHERE id=?", (recalculated_price, sp["id"]))
        recalc_count += 1

    db.commit()
    trigger_catalog_refresh()

    await m.answer(
        f"✅ <b>All Manual Price Overrides Cleared!</b>\n\n"
        f"• Cleared manual price locks: <b>{len(overridden)}</b> product(s)\n"
        f"• Reset to dynamic pricing: <b>{recalc_count}</b> supplier product(s)\n"
        f"• Active store profit margin: <b>{global_margin}%</b>\n\n"
        f"<i>All supplier products are now dynamically calculated from wholesale costs.</i>"
    )


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


@dp.message(Command("broadcastpause", "pausebroadcast"), is_admin)
async def admin_pause_broadcaster(m: Message, command: CommandObject = None):
    set_setting("broadcaster_enabled", "0")
    await m.answer(
        "⏸️ <b>Stock Broadcaster Paused!</b>\n\n"
        "Automated restock broadcasts to channel and buyers are now paused.\n"
        "Incoming supplier restock alerts will be ignored while paused.\n\n"
        "<i>To resume broadcasts at any time, run <code>/broadcast play</code> or <code>/broadcastplay</code>.</i>"
    )


@dp.message(Command("broadcastplay", "playbroadcast"), is_admin)
async def admin_play_broadcaster(m: Message, command: CommandObject = None):
    set_setting("broadcaster_enabled", "1")
    await m.answer(
        "▶️ <b>Stock Broadcaster Resumed!</b>\n\n"
        "Automated restock broadcasts are now active.\n"
        "Incoming supplier restock alerts will be transformed and posted automatically.\n\n"
        "<i>To pause broadcasts at any time, run <code>/broadcast pause</code> or <code>/broadcastpause</code>.</i>"
    )


@dp.message(Command("broadcast"), is_admin)
async def admin_broadcast(m: Message, command: CommandObject = None):
    raw_args = (command.args or "").strip() if command else ""
    args_lower = raw_args.lower()

    if args_lower in ("pause", "stop", "hold", "freeze"):
        return await admin_pause_broadcaster(m, command)

    if args_lower in ("play", "start", "resume", "enable", "on", "unpause"):
        return await admin_play_broadcaster(m, command)

    if args_lower == "toggle":
        if is_broadcaster_enabled():
            return await admin_pause_broadcaster(m, command)
        else:
            return await admin_play_broadcaster(m, command)

    if not raw_args or args_lower in ("status", "info", "check"):
        status_text = "🟢 <b>Active</b>" if is_broadcaster_enabled() else "⏸️ <b>Paused</b>"
        return await m.answer(
            f"📢 <b>Stock Broadcaster Status:</b> {status_text}\n\n"
            f"<b>Controls:</b>\n"
            f"/broadcastpause or /broadcast pause - Pause automated restock broadcasts\n"
            f"/broadcastplay or /broadcast play - Resume automated restock broadcasts\n\n"
            f"<b>Manual Broadcast:</b>\n"
            f"/broadcast &lt;text&gt; - Send message to all registered store buyers"
        )

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


async def send_catalog_document(target, status_msg: Optional[Message] = None):
    """Generate and send the products Excel catalog (or CSV fallback) to a chat or callback."""
    try:
        excel_path = await refresh_catalog_excel(DB_PATH)
        if not excel_path or not os.path.exists(excel_path):
            excel_path = "CreditLog_Bot_Supplier_Products_Catalog.xlsx"
            if not os.path.exists(excel_path):
                excel_path = "CreditLog_Bot_Supplier_Products_Catalog.csv"

        size_kb = os.path.getsize(excel_path) / 1024.0
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        ext = "csv" if excel_path.endswith(".csv") else "xlsx"
        doc = FSInputFile(excel_path, filename=f"CreditLog_Products_{time.strftime('%Y%m%d_%H%M%S')}.{ext}")
        caption = (
            f"📊 <b>Updated Products Catalog ({ext.upper()})</b>\n"
            f"📅 Generated: <code>{ts}</code>\n"
            f"📦 Size: <code>{size_kb:.1f} KB</code>\n\n"
            f"• Wholesale costs, store prices, profit margins, stock & instructions included."
        )
        if hasattr(target, "message") and target.message:
            await target.message.answer_document(doc, caption=caption)
        elif hasattr(target, "answer_document"):
            await target.answer_document(doc, caption=caption)
        if status_msg:
            await status_msg.delete()
    except Exception as e:
        log.exception("Excel catalog export failed: %s", e)
        err_msg = f"❌ Failed to generate Excel catalog: <code>{esc(str(e))}</code>"
        if status_msg:
            await status_msg.edit_text(err_msg)
        elif hasattr(target, "message") and target.message:
            await target.message.answer(err_msg)
        elif hasattr(target, "answer"):
            await target.answer(err_msg)


@dp.message(Command("export"), is_admin)
@dp.message(Command("excel"), is_admin)
async def admin_export_excel(m: Message):
    status_msg = await m.answer("⏳ <i>Generating fresh products Excel catalog...</i>")
    await send_catalog_document(m, status_msg=status_msg)


@dp.callback_query(F.data == "admin_export_excel")
async def cb_admin_export_excel(c: CallbackQuery):
    if not (c.from_user and c.from_user.id in ADMIN_IDS):
        return await c.answer("Unauthorized", show_alert=True)
    await c.answer("Generating catalog document...", show_alert=False)
    await send_catalog_document(c)


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


@dp.message(Command("bachspause", "pausebachs"), is_admin)
async def admin_pause_bachs(m: Message, command: CommandObject = None):
    set_setting("bachs_enabled", "0")
    await m.answer(
        "⏸️ <b>Bachs Payments Paused!</b>\n\n"
        "Bachs checkout is now paused and hidden from customers in checkout.\n\n"
        "<i>To resume payments at any time, run <code>/bachs play</code> or <code>/bachsplay</code>.</i>"
    )


@dp.message(Command("bachsplay", "playbachs"), is_admin)
async def admin_play_bachs(m: Message, command: CommandObject = None):
    # Clear "off" setting if previously set so environment key can be used
    if get_setting("bachs_api_key", "").lower() in ("off", "disable", "disabled", "none", "0"):
        set_setting("bachs_api_key", "")
    key = get_bachs_key()
    has_key = bool(key and key.lower() not in ("off", "disable", "disabled", "none", "0") and not key.startswith("your_") and len(key) >= 10)
    if not has_key:
        return await m.answer(
            "⚠️ <b>Cannot resume Bachs payments:</b> No valid API key is configured.\n\n"
            "Please configure your API key first:\n"
            "<code>/setbachs &lt;api_key&gt;</code>"
        )
    set_setting("bachs_enabled", "1")
    curr = get_bachs_currency()
    env_type = "Sandbox" if "sandbox" in key.lower() or key.startswith("sk_test") else "Live"
    await m.answer(
        "▶️ <b>Bachs Payments Resumed!</b>\n\n"
        "Bachs checkout is now active and available for buyers.\n\n"
        f"• <b>Environment:</b> <code>{env_type}</code>\n"
        f"• <b>Currency:</b> <code>{curr}</code>\n\n"
        "<i>To pause payments at any time, run <code>/bachs pause</code> or <code>/bachspause</code>.</i>"
    )


@dp.message(Command("delbachs", "clearbachs"), is_admin)
@dp.message(Command("setbachs"), is_admin)
async def admin_setbachs(m: Message, command: CommandObject = None):
    cmd_name = command.command.lower() if command and command.command else ""
    raw_args = (command.args or "").strip() if command else ""
    args_lower = raw_args.lower()
    if cmd_name in ("delbachs", "clearbachs") or args_lower in ("off", "disable", "clear", "del", "delete", "none", "0"):
        set_setting("bachs_api_key", "off")
        return await m.answer(
            "✅ <b>Bachs Payment Gateway Disabled!</b>\n\n"
            "Bachs checkout has been deactivated. The Bachs payment button will no longer appear in the checkout menu.\n\n"
            "<i>To re-enable at any time, run <code>/setbachs &lt;api_key&gt;</code></i>"
        )
    if args_lower in ("pause", "stop", "hold", "freeze"):
        return await admin_pause_bachs(m, command)
    if args_lower in ("play", "start", "resume", "enable", "on", "unpause"):
        return await admin_play_bachs(m, command)
    if args_lower == "toggle":
        if is_bachs_enabled():
            return await admin_pause_bachs(m, command)
        else:
            return await admin_play_bachs(m, command)
    if args_lower.startswith("currency") or args_lower.startswith("curr"):
        c_parts = raw_args.split(None, 1)
        sub_arg = c_parts[1].strip() if len(c_parts) > 1 else ""
        c_obj = CommandObject(prefix="/", command="bachscurrency", args=sub_arg)
        return await admin_bachs_currency(m, c_obj)

    if not raw_args or args_lower in ("status", "info", "view", "check", "help"):
        key = get_bachs_key()
        has_key = bool(key and key.lower() not in ("off", "disable", "disabled", "none", "0") and not key.startswith("your_") and len(key) >= 10)
        if not has_key:
            status_text = "🔴 <b>Not Configured</b>"
        elif not is_bachs_enabled():
            status_text = "⏸️ <b>Paused (Checkout Disabled)</b>"
        else:
            status_text = "🟢 <b>Configured & Active</b>"

        masked = f"{key[:8]}...{key[-4:]}" if len(key) > 12 else ("(not set)" if not key else "***")
        curr = get_bachs_currency()
        env_type = "Sandbox" if "sandbox" in key.lower() or key.startswith("sk_test") else "Live"
        return await m.answer(
            f"💳 <b>Bachs Payment Gateway Status:</b> {status_text}\n\n"
            f"• <b>API Key:</b> <code>{masked}</code>\n"
            f"• <b>Environment:</b> <code>{env_type}</code>\n"
            f"• <b>Currency:</b> <code>{curr}</code>\n"
            f"• <b>Base URL:</b> <code>{get_bachs_base_url()}</code>\n\n"
            f"<b>Controls:</b>\n"
            f"/bachspause or /bachs pause - Pause Bachs payments\n"
            f"/bachsplay or /bachs play - Resume Bachs payments\n"
            f"/bachscurrency USD|NGN or /bachs currency USD|NGN - Set billing currency\n"
            f"/setbachs &lt;api_key&gt; - Update API key\n\n"
            f"<i>Get your API key (sk_live_... or sk_sandbox_...) from your dashboard at https://app.bachs.io</i>"
        )

    # If called via /bachs with unrecognized text that isn't an API key, don't overwrite key
    is_likely_key = raw_args.startswith("sk_") or (len(raw_args) >= 15 and " " not in raw_args)
    if cmd_name not in ("setbachs", "bachskey") and not is_likely_key:
        return await m.answer(
            "⚠️ <b>Unknown Bachs command option.</b>\n\n"
            "<b>Available Bachs commands:</b>\n"
            "/bachs - View status\n"
            "/bachspause or /bachs pause - Pause Bachs checkouts\n"
            "/bachsplay or /bachs play - Resume Bachs checkouts\n"
            "/bachscurrency USD|NGN - Change currency\n"
            "/setbachs &lt;api_key&gt; - Update Bachs API key\n"
            "/delbachs - Disable Bachs gateway"
        )
    set_setting("bachs_api_key", raw_args)
    set_setting("bachs_enabled", "1")
    env_type = "Sandbox" if "sandbox" in raw_args.lower() or raw_args.startswith("sk_test") else "Live"
    await m.answer(
        f"✅ <b>Bachs API Key Updated!</b>\n\n"
        f"• <b>Environment:</b> <code>{env_type}</code>\n"
        f"• <b>Base URL:</b> <code>{get_bachs_base_url(raw_args)}</code>\n\n"
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
    purge_delisted: bool = False,
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

            # Sync strictly by supplier_product_id from supplier API
            pass

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

        # Clear legacy unlinked cached products
        db.execute("DELETE FROM products WHERE supplier_product_id IS NULL")

        if purge_delisted and seen_supplier_ids:
            placeholders = ",".join("?" for _ in seen_supplier_ids)
            db.execute(
                f"DELETE FROM products WHERE supplier=? AND supplier_product_id NOT IN ({placeholders})",
                (sup_name, *seen_supplier_ids)
            )
        elif seen_supplier_ids:
            # Deactivate supplier products no longer in feed
            cur_prods = db.execute(
                "SELECT id, supplier_product_id FROM products WHERE supplier=? AND delivery_type='auto' AND supplier_product_id IS NOT NULL",
                (sup_name,)
            ).fetchall()
            for cp in cur_prods:
                if str(cp["supplier_product_id"]) not in seen_supplier_ids:
                    db.execute("UPDATE products SET supplier_stock=0, active=0 WHERE id=?", (cp["id"],))

        total_items += len(sup_products)

    try:
        from retail_catalog import sync_retail_catalog_details
        sync_retail_catalog_details(db)
        deduplicate_products_in_db(db)
    except Exception as e:
        log.warning("Post-supplier-sync retail catalog sync failed: %s", e)

    return total_added, total_updated, total_items, errors


@dp.message(Command("s_sync"), is_admin)
async def admin_supplier_sync(m: Message, command: CommandObject = None):
    registered = supplier_registry.list_suppliers()
    if not registered:
        return await m.answer("⚠️ No suppliers configured in .env.")

    args = (command.args or "").strip().split() if command else []
    target_suppliers = []
    markup = get_default_margin()
    purge_delisted = False

    if args and args[0].lower() in ("clear", "clean", "reset", "purge"):
        purge_delisted = True
        db.execute("DELETE FROM products WHERE supplier_product_id IS NULL")
        db.commit()
        args = args[1:]

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

    total_added, total_updated, total_items, errors = await perform_supplier_sync(target_suppliers, markup, purge_delisted=purge_delisted)

    # Sync supplier retail bot catalog items as well
    try:
        from retail_catalog import seed_retail_catalog
        retail_added = seed_retail_catalog(db)
        total_added += retail_added
        deduplicate_products_in_db(db)
    except Exception as e:
        logger.warning(f"Retail SKUs sync in /s_sync skipped: {e}")

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

    trigger_catalog_refresh()


@dp.message(Command("clearcache", "clearcatalog"), is_admin)
async def admin_clearcache(m: Message, command: CommandObject = None):
    """Purge all cached/unlinked products and perform a clean 100% sync from the live supplier API."""
    db.execute("DELETE FROM products WHERE supplier_product_id IS NULL")
    db.commit()

    total_added, total_updated, total_items, errors = await perform_supplier_sync(purge_delisted=True)

    try:
        from retail_catalog import seed_retail_catalog
        seed_retail_catalog(db)
        resequence_product_ids(db)
    except Exception as e:
        logger.warning(f"Retail SKUs re-seed skipped: {e}")

    trigger_catalog_refresh()

    admin_curr = get_user_currency(m.from_user.id)
    active_count = db.execute("SELECT COUNT(*) FROM products WHERE active=1").fetchone()[0]
    total_count = db.execute("SELECT COUNT(*) FROM products").fetchone()[0]

    err_text = f"\n⚠️ Errors: {', '.join(errors)}" if errors else ""
    await m.answer(
        f"🧹 <b>Cache Cleared & Supplier API Synced</b>\n\n"
        f"• All manual cached products removed.\n"
        f"• Catalog synced 100% directly from Supplier API.\n"
        f"• Live Products Processed: <b>{total_items}</b>\n"
        f"• Active In-Stock in Shop: <b>{active_count}</b> / <b>{total_count}</b>\n"
        f"• Currency: <b>{admin_curr}</b>{err_text}"
    )


@dp.message(Command("sync_retail"), is_admin)
async def admin_sync_retail(m: Message, command: CommandObject = None):
    """Sync retail catalog rich descriptions and live stock from retail_catalog_details.json."""
    status_msg = await m.answer("⏳ <i>Syncing retail catalog from retail_catalog_details.json...</i>")
    try:
        from retail_catalog import sync_retail_catalog_details
        updated, inserted = sync_retail_catalog_details(db)
        resequence_product_ids(db)
        trigger_catalog_refresh()
        await status_msg.edit_text(
            f"✅ <b>Retail Catalog Synced Successfully!</b>\n\n"
            f"• <b>Updated:</b> {updated} products (full descriptions & live stock)\n"
            f"• <b>Inserted:</b> {inserted} new products\n"
            f"• <b>Resequenced:</b> Clean 1..N IDs maintained."
        )
    except Exception as e:
        await status_msg.edit_text(f"❌ <b>Sync failed:</b> {e}")


_crawler_lock = asyncio.Lock()


@dp.message(Command("crawl_retail"), is_admin)
async def admin_crawl_retail(m: Message, command: CommandObject = None):
    """Scrapes all pages from the supplier retail bot, discovers new SKUs and updates live stock."""
    if _crawler_lock.locked():
        return await m.answer("⚠️ <b>A retail catalog crawl is already in progress.</b> Please wait for it to finish.")

    args = (command.args or "").strip().lower() if command else ""
    force_refresh = "force" in args

    status_msg = await m.answer(
        f"⏳ <b>Starting Retail Catalog Crawler{' (FORCE REFRESH)' if force_refresh else ''}...</b>\n\n"
        f"Connecting to supplier bot and scanning catalog pages..."
    )

    async def progress_update(page: int, total: int, text: str):
        try:
            if page == 1 or page % 4 == 0 or page == total:
                await status_msg.edit_text(
                    f"⏳ <b>Retail Catalog Crawler Running...</b>\n\n"
                    f"• <b>Progress:</b> Page {page}/{total}\n"
                    f"• <i>{esc(text)}</i>"
                )
        except Exception:
            pass

    async with _crawler_lock:
        try:
            from crawl_retail_details import run_retail_crawler
            res = await run_retail_crawler(db_path=DB_PATH, progress_cb=progress_update, force_refresh_all=force_refresh)

            if res.get("success"):
                deduplicate_products_in_db(db)
                resequence_product_ids(db)
                trigger_catalog_refresh()
                await status_msg.edit_text(
                    f"✅ <b>Retail Catalog Crawl Complete!</b>\n\n"
                    f"• <b>Pages Scanned:</b> {res.get('pages_scanned', 19)}\n"
                    f"• <b>Total Products in Catalog:</b> {res.get('total_scraped', 0)}\n"
                    f"• <b>New Products Added:</b> {res.get('new_items', 0)}\n"
                    f"• <b>Products Updated:</b> {res.get('updated_items', 0)}\n"
                    f"• <b>Status:</b> All rich descriptions and stock counts are live!"
                )
            else:
                await status_msg.edit_text(f"❌ <b>Crawl failed:</b> {esc(res.get('error', 'Unknown error'))}")
        except Exception as e:
            await status_msg.edit_text(f"❌ <b>Crawler encountered an error:</b> {esc(str(e))}")


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
            await refresh_catalog_excel()
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("Periodic supplier sync encountered error: %s", e)


async def periodic_retail_crawler_task(aiogram_bot: Bot):
    """Background task to periodically crawl supplier retail bot for new products and live stock."""
    # Delay initial crawl by 60 seconds after startup to allow primary services to boot
    await asyncio.sleep(60)
    while True:
        try:
            if get_setting("auto_crawler_enabled", "1") not in ("0", "false", "paused", "off"):
                log.info("Starting scheduled retail catalog crawl...")
                from crawl_retail_details import run_retail_crawler
                res = await run_retail_crawler(db_path=DB_PATH)
                if res.get("success"):
                    resequence_product_ids(db)
                    trigger_catalog_refresh()
                    new_items = res.get("new_items", 0)
                    log.info(
                        "Scheduled retail crawl finished: %d total, %d new, %d updated",
                        res.get("total_scraped", 0),
                        new_items,
                        res.get("updated_items", 0)
                    )
                    if new_items > 0:
                        for aid in ADMIN_IDS:
                            try:
                                await aiogram_bot.send_message(
                                    aid,
                                    f"📢 <b>Auto-Crawler Alert:</b> Found <b>{new_items} new retail SKU(s)</b> from supplier!\n"
                                    f"Total catalog: <b>{res.get('total_scraped', 0)} products</b>."
                                )
                            except Exception:
                                pass
                else:
                    log.warning("Scheduled retail crawl returned failure: %s", res.get("error"))
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("Periodic retail crawler encountered error: %s", e)

        interval_hours = float(get_setting("crawler_interval_hours", "2"))
        await asyncio.sleep(max(1800, int(interval_hours * 3600)))


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
    register_web_routes(app, sys.modules[__name__])
    app[TELEGRAM_BOT_KEY] = bot
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Health check server listening on :%s", PORT)

    # Sync catalog on startup
    try:
        init_added, init_updated, init_total, init_errs = await perform_supplier_sync()
        log.info("Initial supplier sync: %d updated, %d added of %d total (errors: %d)", init_updated, init_added, init_total, len(init_errs))
        await refresh_catalog_excel()
    except Exception as e:
        log.warning("Initial supplier sync on startup failed: %s", e)

    sync_task = asyncio.create_task(periodic_supplier_sync(600))
    broadcaster_task = None
    try:
        from broadcaster.broadcaster import start_broadcaster_task
        broadcaster_task = asyncio.create_task(start_broadcaster_task(aiogram_bot=bot))
    except Exception as e:
        log.info("Stock broadcaster could not be initialized: %s", e)

    crawler_task = None
    try:
        crawler_task = asyncio.create_task(periodic_retail_crawler_task(aiogram_bot=bot))
    except Exception as e:
        log.info("Periodic retail crawler could not be initialized: %s", e)

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
            BotCommand(command="start", description="Start bot / Open store"),
            BotCommand(command="admin", description="Admin command center"),
            BotCommand(command="featured", description="Manage homepage featured products"),
            BotCommand(command="mode", description="Switch Model A / Model B fulfillment"),
            BotCommand(command="backup", description="Download database backup (.db)"),
            BotCommand(command="export", description="Download updated products Excel (.xlsx)"),
            BotCommand(command="stats", description="View store sales stats"),
            BotCommand(command="products", description="List products & margins"),
            BotCommand(command="categories", description="View & manage store categories"),
            BotCommand(command="addcategory", description="Add or update a store category"),
            BotCommand(command="delcategory", description="Remove a store category"),
            BotCommand(command="clearprices", description="Clear manual price overrides"),
            BotCommand(command="clearcache", description="Clear cached products & sync from supplier API"),
            BotCommand(command="sync_retail", description="Sync retail catalog descriptions & stock"),
            BotCommand(command="crawl_retail", description="Crawl supplier bot for new SKUs & live stock"),
            BotCommand(command="s_products", description="List supplier products"),
            BotCommand(command="s_sync", description="Sync supplier catalog"),
            BotCommand(command="s_orders", description="Recent supplier orders"),
            BotCommand(command="supplier", description="Supplier balance & info"),
            BotCommand(command="setrate", description="Update USD/NGN exchange rate"),
            BotCommand(command="setbachs", description="Configure Bachs API Key"),
            BotCommand(command="bachs", description="View Bachs Gateway details & status"),
            BotCommand(command="bachsplay", description="Play/resume Bachs payments"),
            BotCommand(command="bachspause", description="Pause Bachs payments"),
            BotCommand(command="setbank", description="Configure Direct Bank Transfer"),
            BotCommand(command="addbank", description="Add additional bank account"),
            BotCommand(command="delbank", description="Remove a bank account"),
            BotCommand(command="bank", description="View Bank Transfer details"),
            BotCommand(command="fsub", description="Manage Force-Subscribe"),
            BotCommand(command="broadcast", description="Broadcast message to buyers"),
            BotCommand(command="broadcastplay", description="Play/resume automated stock broadcasts"),
            BotCommand(command="broadcastpause", description="Pause automated stock broadcasts"),
        ]
        for aid in ADMIN_IDS:
            try:
                await bot.set_my_commands(admin_cmd_list, scope=BotCommandScopeChat(chat_id=aid))
            except Exception as e:
                log.debug("Could not set admin commands for chat %s: %s", aid, e)

        await dp.start_polling(bot)
    finally:
        sync_task.cancel()
        if broadcaster_task:
            broadcaster_task.cancel()
        if crawler_task:
            crawler_task.cancel()
        try:
            await sync_task
        except asyncio.CancelledError:
            pass
        if broadcaster_task:
            try:
                await broadcaster_task
            except asyncio.CancelledError:
                pass
        if crawler_task:
            try:
                await crawler_task
            except asyncio.CancelledError:
                pass
        await supplier_registry.close_all()
        await reseller.close()
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
