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
from typing import Optional, Tuple

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

from reseller import ResellerClient, ResellerAPIError

load_dotenv()
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("store")

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
TOKEN = os.environ["BOT_TOKEN"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "").replace(" ", "").split(",") if x}
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

# Reseller / Supplier Catalog API
RESELLER_BASE_URL = (os.environ.get("RESELLER_BASE_URL") or "https://api-geminipro.ignorelist.com/api/reseller/v1").rstrip("/")
RESELLER_API_KEY = os.environ.get("RESELLER_API_KEY", "")
reseller = ResellerClient(base_url=RESELLER_BASE_URL, api_key=RESELLER_API_KEY)

# Crypto Payments
CRYPTOBOT_TOKEN = os.environ.get("CRYPTOBOT_TOKEN", "")
BINANCE_PAY_ID = os.environ.get("BINANCE_PAY_ID", "")

# USDT Addresses
CRYPTO_USDT_BEP20 = os.environ.get("CRYPTO_USDT_BEP20", "")
CRYPTO_USDT_TRC20 = os.environ.get("CRYPTO_USDT_TRC20", "")

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
  category TEXT DEFAULT 'AI Tools');
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
  status TEXT DEFAULT 'completed',
  created TEXT DEFAULT CURRENT_TIMESTAMP);
""")

# --------------------------------------------------------------------------
# Categories & Icons Definition (24 Categories from Reference Design)
# --------------------------------------------------------------------------
CATEGORIES = [
    ("chatgpt", "ChatGPT", "🤖"),
    ("gemini", "Gemini", "✨"),
    ("grok", "Grok", "🧠"),
    ("codex", "Codex API", "🤖"),
    ("claude", "Claude", "✴️"),
    ("kimi", "Kimi", "🌙"),
    ("video_ai", "Video AI", "▶️"),
    ("elevenlabs", "ElevenLabs", "🎙"),
    ("ai_tools", "AI Tools", "🧬"),
    ("qwen", "Qwen API", "🔮"),
    ("deepseek", "DeepSeek AI", "🐳"),
    ("cursor", "Cursor API", "⬡"),
    ("adobe", "Adobe", "🅰️"),
    ("autodesk", "Autodesk", "📐"),
    ("design", "Design", "🎨"),
    ("office", "Office", "🪟"),
    ("education", "Education", "🦉"),
    ("streaming", "Streaming", "🍿"),
    ("music", "Music", "🎵"),
    ("vpn", "VPN", "🛡"),
    ("security", "Security", "🔐"),
    ("games", "Games", "🎮"),
    ("social", "Social", "👥"),
    ("discord", "Discord", "👾"),
]
CAT_BY_KEY = {k: (name, icon) for k, name, icon in CATEGORIES}
CAT_KEY_BY_NAME = {name.lower(): k for k, name, icon in CATEGORIES}


def detect_category(name: str) -> str:
    n = name.lower()
    if "codex" in n:
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
    if any(k in n for k in ["sora", "runway", "pika", "kling", "luma", "video ai"]):
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
    if any(k in n for k in ["office", "windows", "microsoft 365", "excel", "word"]):
        return "Office"
    if any(k in n for k in ["duolingo", "coursera", "udemy", "education", "chegg", "quizlet", "cambly"]):
        return "Education"
    if any(k in n for k in ["netflix", "disney", "hbo", "prime video", "streaming", "hulu", "crunchyroll", "youtube"]):
        return "Streaming"
    if any(k in n for k in ["spotify", "apple music", "tidal", "deezer", "music"]):
        return "Music"
    if any(k in n for k in ["vpn", "nordvpn", "expressvpn", "surfshark", "mullvad"]):
        return "VPN"
    if any(k in n for k in ["security", "antivirus", "malwarebytes", "kaspersky", "eset"]):
        return "Security"
    if any(k in n for k in ["steam", "playstation", "xbox", "nintendo", "game", "roblox"]):
        return "Games"
    if any(k in n for k in ["twitter", "x.com", "instagram", "tiktok", "facebook", "linkedin", "social"]):
        return "Social"
    if "discord" in n or "nitro" in n:
        return "Discord"
    return "AI Tools"


def get_product_icon(name: str, cat_name: Optional[str] = None) -> str:
    category = cat_name or detect_category(name)
    key = CAT_KEY_BY_NAME.get(category.lower())
    if key and key in CAT_BY_KEY:
        return CAT_BY_KEY[key][1]
    for _, c_name, icon in CATEGORIES:
        if c_name.lower() == category.lower():
            return icon
    return "📦"


def get_products_for_category(key: str) -> list[sqlite3.Row]:
    cat_name, _ = CAT_BY_KEY.get(key, ("AI Tools", "🧬"))
    all_active = db.execute("SELECT * FROM products WHERE active=1 ORDER BY id").fetchall()
    matched = []
    for p in all_active:
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

    cols_o = {row["name"] for row in db.execute("PRAGMA table_info(orders)").fetchall()}
    if "supplier_order_id" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN supplier_order_id TEXT DEFAULT NULL")
    if "status" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'completed'")
    if "invoice_id" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN invoice_id TEXT DEFAULT NULL")
    if "quantity" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN quantity INTEGER DEFAULT 1")

    # Update categories for existing products if not assigned
    for row in db.execute("SELECT id, name, category FROM products").fetchall():
        if not row["category"] or row["category"] == "AI Tools":
            detected = detect_category(row["name"])
            if detected != "AI Tools":
                db.execute("UPDATE products SET category=? WHERE id=?", (detected, row["id"]))

migrate_db()

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
        "btn_orders": "📦 Purchases",
        "btn_lang": "🌐 Language",
        "btn_support": "💬 Support",
        "btn_news": "📢 News channel",
        "btn_back": "↩️ Back",
        "btn_refresh": "🔄 Refresh",
        "btn_custom_qty": "✏️ Custom Qty",
        "btn_all_items": "🛍 All items",
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
        "orders_title": "📦 <b>Your Recent Purchases</b>",
        "no_orders": "You haven't made any purchases yet. Browse the catalog to get started!",
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


def btn(text, data=None, url=None):
    return InlineKeyboardButton(text=text, callback_data=data, url=url)


def stock_count(pid: int) -> int:
    return db.execute("SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0", (pid,)).fetchone()[0]


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
    top_prods = db.execute("SELECT * FROM products WHERE active=1 ORDER BY id LIMIT 2").fetchall()
    rows = []
    badges = ["1️⃣", "2️⃣"]
    for i, p in enumerate(top_prods):
        badge = badges[i] if i < len(badges) else "⭐"
        rows.append([btn(f"{badge} {p['name']} · {usd(p['price'])}", f"p:{p['id']}:home")])

    rows += [
        [btn(t(uid, "btn_shop"), "shop"), btn(t(uid, "btn_orders"), "orders")],
        [btn(t(uid, "btn_lang"), "lang"), btn(t(uid, "btn_support"), url=SUPPORT_URL)],
        [btn(t(uid, "btn_news"), url=CHANNEL_URL)],
    ]
    return kb(rows)


def categories_kb(uid: int):
    """3-column category grid matching Screenshot 3."""
    rows = []
    current_row = []
    for key, name, icon in CATEGORIES:
        current_row.append(btn(f"{icon} {name}", f"cat:{key}:1"))
        if len(current_row) == 3:
            rows.append(current_row)
            current_row = []
    if current_row:
        rows.append(current_row)

    rows.append([btn(t(uid, "btn_search"), "search_start"), btn(t(uid, "btn_all_items"), "allprods:1")])
    rows.append([btn(t(uid, "btn_back"), "home")])
    return kb(rows)


PAGE_SIZE = 12


def render_product_list(uid: int, prods: list, page: int, cb_prefix: str, ctx_prefix: str, back_cb: str) -> Tuple[str, InlineKeyboardMarkup]:
    """1-column product list matching Screenshot 4."""
    total_pages = max(1, (len(prods) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))

    if not prods:
        return t(uid, "no_products"), kb([[btn(t(uid, "btn_back"), back_cb)]])

    slice_prods = prods[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    rows = []
    for p in slice_prods:
        p_cat = p["category"] if "category" in p.keys() else None
        icon = get_product_icon(p["name"], p_cat)
        rows.append([btn(f"{icon} {p['name']} · {usd(p['price'])} ✅", f"p:{p['id']}:{ctx_prefix}_{page}")])

    if total_pages > 1:
        pag_row = []
        if page > 1:
            pag_row.append(btn("◀️", f"{cb_prefix}:{page - 1}"))
        pag_row.append(btn(f"{page}/{total_pages}", "noop"))
        if page < total_pages:
            pag_row.append(btn("▶️", f"{cb_prefix}:{page + 1}"))
        rows.append(pag_row)

    rows.append([
        btn(t(uid, "btn_refresh"), f"{cb_prefix}:{page}"),
        btn(t(uid, "btn_back"), back_cb)
    ])
    return t(uid, "prods_title"), kb(rows)


def render_product_card(uid: int, p: sqlite3.Row, ctx: str) -> Tuple[str, InlineKeyboardMarkup]:
    """Detailed product card and quantity selector matching Screenshot 1."""
    is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
    count = stock_count(p["id"]) if not is_auto else None

    if is_auto:
        stock_line = f"⚡ <b>{t(uid, 'stock_auto')}</b>"
    elif count and count > 0:
        stock_line = f"📦 In stock: {count} pcs"
    else:
        stock_line = "❌ Out of stock"

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
    has_stock = is_auto or (count and count > 0)
    if has_stock:
        qty_row = []
        qty_row.append(btn("1", f"buyqty:{p['id']}:1:{ctx}"))
        if is_auto:
            qty_row.append(btn("5", f"buyqty:{p['id']}:5:{ctx}"))
            qty_row.append(btn("10", f"buyqty:{p['id']}:10:{ctx}"))
        else:
            if count >= 5:
                qty_row.append(btn("5", f"buyqty:{p['id']}:5:{ctx}"))
                qty_row.append(btn(f"All {count}", f"buyqty:{p['id']}:{count}:{ctx}"))
            elif count > 1:
                qty_row.append(btn(f"All {count}", f"buyqty:{p['id']}:{count}:{ctx}"))
        rows.append(qty_row)
        rows.append([btn(t(uid, "btn_custom_qty"), f"cqty:{p['id']}:{ctx}")])

    # Resolve back navigation
    if ctx == "home":
        back_cb = "home"
    elif ctx.startswith("c_"):
        parts = ctx.split("_")
        back_cb = f"cat:{parts[1]}:{parts[2]}"
    elif ctx.startswith("all_"):
        parts = ctx.split("_")
        back_cb = f"allprods:{parts[1]}"
    elif ctx.startswith("s_"):
        parts = ctx.split("_", 2)
        term = parts[1]
        page = parts[2] if len(parts) > 2 else "1"
        back_cb = f"srch:{term}:{page}"
    else:
        back_cb = "shop"

    rows.append([
        btn(t(uid, "btn_refresh"), f"p:{p['id']}:{ctx}"),
        btn(t(uid, "btn_back"), back_cb)
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
        try:
            order_res = await reseller.create_order(product_id=p["supplier_product_id"], quantity=qty)
            payload = order_res.get("value")
            supplier_order_id = order_res.get("order_code")
            if not payload:
                raise ResellerAPIError(f"Supplier order succeeded but returned empty value: {order_res}")
        except Exception as e:
            log.exception("Supplier order failed for order %s", oid)
            db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
            await notify_admins(bot, f"⚠️ Supplier order failed for order #{oid} (user {o['user_id']}, product #{p['id']}, qty {qty}):\n<code>{esc(str(e))}</code>")
            return None, "supplier_error"

        db.execute(
            "UPDATE orders SET payload=?, supplier_order_id=?, status='completed' WHERE id=?",
            (payload, supplier_order_id, oid)
        )
        return payload, None

    # Option B: Local Stock Items
    db.execute("BEGIN IMMEDIATE")
    try:
        s_items = db.execute(
            "SELECT * FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT ?",
            (p["id"], qty)
        ).fetchall()
        if len(s_items) < qty:
            db.execute("ROLLBACK")
            db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
            return None, "out_of_stock"
        ids = [item["id"] for item in s_items]
        db.execute(f"UPDATE stock SET sold=1 WHERE id IN ({','.join(['?']*len(ids))})", ids)
        payload = "\n".join(item["payload"] for item in s_items)
        db.execute("UPDATE orders SET payload=?, status='completed' WHERE id=?", (payload, oid))
        db.execute("COMMIT")
        return payload, None
    except Exception:
        db.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# User Handlers & Navigation
# --------------------------------------------------------------------------
@dp.message(CommandStart())
async def start(m: Message):
    pending_inputs.pop(m.from_user.id, None)
    get_user(m.from_user.id, m.from_user.language_code)
    await m.answer(t(m.from_user.id, "welcome"), reply_markup=home_kb(m.from_user.id))


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
    _, key, page_str = c.data.split(":")
    page = int(page_str)
    prods = get_products_for_category(key)
    text, markup = render_product_list(
        uid, prods, page=page,
        cb_prefix=f"cat:{key}",
        ctx_prefix=f"c_{key}",
        back_cb="shop"
    )
    await edit(c, text, markup)


@dp.callback_query(F.data.startswith("allprods:"))
async def on_all_prods(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    page = int(c.data.split(":")[1])
    prods = db.execute("SELECT * FROM products WHERE active=1 ORDER BY id").fetchall()
    text, markup = render_product_list(
        uid, prods, page=page,
        cb_prefix="allprods",
        ctx_prefix="all",
        back_cb="shop"
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
    _, term, page_str = c.data.split(":", 2)
    page = int(page_str)
    prods = db.execute(
        "SELECT * FROM products WHERE active=1 AND (LOWER(name) LIKE ? OR LOWER(description) LIKE ?) ORDER BY id",
        (f"%{term.lower()}%", f"%{term.lower()}%")
    ).fetchall()
    text, markup = render_product_list(
        uid, prods, page=page,
        cb_prefix=f"srch:{term}",
        ctx_prefix=f"s_{term}",
        back_cb="shop"
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
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
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


async def start_checkout(event: Message | CallbackQuery, uid: int, p: sqlite3.Row, qty: int, ctx: str):
    """Guest checkout with quantity support."""
    pid = p["id"]
    is_auto = bool(p["auto_fulfill"] and p["supplier_product_id"])
    if not is_auto:
        avail = stock_count(pid)
        if avail < qty:
            msg = f"⚠️ Not enough stock available ({avail} left)."
            if isinstance(event, CallbackQuery):
                return await event.answer(msg, show_alert=True)
            return await event.answer(msg)

    total_cents = p["price"] * qty

    # Mode 1: Telegram @CryptoBot gateway enabled
    if CRYPTOBOT_TOKEN and not CRYPTOBOT_TOKEN.startswith("token-from"):
        cur = db.execute(
            "INSERT INTO orders(user_id, product_id, product_name, price, quantity, status) VALUES(?,?,?,?,?,'pending')",
            (uid, pid, p["name"], total_cents, qty)
        )
        oid = cur.lastrowid
        try:
            inv = await create_cryptobot_invoice(oid, f"{p['name']} x{qty}", total_cents)
            inv_id = inv["invoice_id"]
            db.execute("UPDATE orders SET invoice_id=? WHERE id=?", (str(inv_id), oid))
            pay_url = inv.get("bot_invoice_url") or inv.get("pay_url")
        except Exception:
            log.exception("CryptoBot invoice creation failed")
            db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
            err_msg = t(uid, "checkout_unavailable")
            if isinstance(event, CallbackQuery):
                return await event.answer(err_msg, show_alert=True)
            return await event.answer(err_msg)

        rows = [
            [btn(t(uid, "btn_pay_cb", amount=usd(total_cents)), url=pay_url)],
            [btn(t(uid, "btn_check_pay"), f"chk_cb:{oid}:{inv_id}")],
        ]
        if has_manual_crypto():
            rows.append([btn(t(uid, "btn_manual_crypto"), f"manual_pay:{oid}")])
        rows.append([btn(t(uid, "btn_back"), f"p:{pid}:{ctx}")])

        text = t(uid, "checkout_invoice", oid=oid, name=f"{esc(p['name'])} (x{qty})", amount=usd(total_cents))
        if isinstance(event, CallbackQuery):
            await edit(event, text, kb(rows))
        else:
            await event.answer(text, reply_markup=kb(rows))
        return

    # Mode 2: Manual Crypto Transfer (Binance Pay / USDT addresses)
    if has_manual_crypto():
        cur = db.execute(
            "INSERT INTO orders(user_id, product_id, product_name, price, quantity, status) VALUES(?,?,?,?,?,'pending')",
            (uid, pid, p["name"], total_cents, qty)
        )
        oid = cur.lastrowid
        details = manual_crypto_details()
        text = t(uid, "manual_crypto_info", oid=oid, name=f"{esc(p['name'])} (x{qty})", amount=usd(total_cents), details=details)
        markup = kb([[btn(t(uid, "btn_support"), url=SUPPORT_URL)],
                     [btn(t(uid, "btn_back"), f"p:{pid}:{ctx}")]])
        if isinstance(event, CallbackQuery):
            await edit(event, text, markup)
        else:
            await event.answer(text, reply_markup=markup)
        return

    # Mode 3: Direct fulfillment mode (testing or free instant checkout)
    cur = db.execute(
        "INSERT INTO orders(user_id, product_id, product_name, price, quantity, status) VALUES(?,?,?,?,?,'processing')",
        (uid, pid, p["name"], total_cents, qty)
    )
    oid = cur.lastrowid
    bot_inst = event.bot
    payload, err = await fulfill_order(bot_inst, oid)
    if err:
        err_msg = t(uid, err)
        if isinstance(event, CallbackQuery):
            return await event.answer(err_msg, show_alert=True)
        return await event.answer(err_msg)

    if isinstance(event, CallbackQuery):
        await event.message.answer(t(uid, "purchased", oid=oid, payload=esc(payload)))
        await event.answer()
    else:
        await event.answer(t(uid, "purchased", oid=oid, payload=esc(payload)))

    if not is_auto:
        left = stock_count(pid)
        if left <= LOW_STOCK:
            name = db.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()["name"]
            await notify_admins(bot_inst, f"⚠️ Low stock: <b>{esc(name)}</b> (id {pid}) has {left} left.")


@dp.callback_query(F.data.startswith("buyqty:"))
async def on_buy_qty(c: CallbackQuery):
    pending_inputs.pop(c.from_user.id, None)
    uid = c.from_user.id
    _, pid_str, qty_str, ctx = c.data.split(":", 3)
    pid, qty = int(pid_str), int(qty_str)
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)
    await start_checkout(c, uid, p, qty, ctx)


@dp.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    """Backward compatibility for single quantity checkout."""
    pending_inputs.pop(c.from_user.id, None)
    uid, pid = c.from_user.id, int(c.data[4:])
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)
    await start_checkout(c, uid, p, qty=1, ctx="shop")


@dp.callback_query(F.data.startswith("cqty:"))
async def on_custom_qty(c: CallbackQuery):
    uid = c.from_user.id
    parts = c.data.split(":", 2)
    pid = int(parts[1])
    ctx = parts[2] if len(parts) > 2 else "shop"
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
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
        if not p:
            return await m.answer(t(uid, "out_of_stock"), reply_markup=kb([[btn(t(uid, "btn_back"), "shop")]]))

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
            "SELECT * FROM products WHERE active=1 AND (LOWER(name) LIKE ? OR LOWER(description) LIKE ?) ORDER BY id",
            (f"%{term.lower()}%", f"%{term.lower()}%")
        ).fetchall()
        text, markup = render_product_list(
            uid, prods, page=1,
            cb_prefix=f"srch:{term}",
            ctx_prefix=f"s_{term}",
            back_cb="shop"
        )
        await m.answer(text, reply_markup=markup)


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
        if payload:
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
    await edit(c, text, kb([[btn(t(uid, "btn_support"), url=SUPPORT_URL)],
                            [btn(t(uid, "btn_back"), f"p:{o['product_id']}")] ]))


@dp.callback_query(F.data == "orders")
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

    markup = kb([[btn(t(uid, "btn_back"), "home")]])
    if isinstance(event, CallbackQuery):
        await edit(event, text, markup)
    else:
        await event.answer(text, reply_markup=markup)


@dp.callback_query(F.data == "lang")
async def lang_menu(c: CallbackQuery):
    uid = c.from_user.id
    rows = [[btn(name, f"setlang:{code}")] for code, name in LANG_NAMES.items()]
    rows.append([btn(t(uid, "btn_back"), "home")])
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
def is_admin(m: Message) -> bool:
    return m.from_user.id in ADMIN_IDS


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
        "🌐 <b>Supplier Catalog (Automated)</b>\n"
        "/supplier - Supplier balance & account info\n"
        "/s_products - List available supplier products\n"
        "/s_sync [markup_%] - Auto-sync catalog with profit margin\n"
        "/s_link &lt;local_id&gt; &lt;supplier_id&gt; - Link item to supplier\n"
        "/s_unlink &lt;local_id&gt; - Revert to manual stock\n"
        "/s_orders - Recent supplier orders"
    )


@dp.message(Command("products"), is_admin)
async def admin_products(m: Message):
    rows = db.execute("SELECT * FROM products ORDER BY id").fetchall()
    if not rows:
        return await m.answer("No products.")
    lines = []
    for p in rows:
        if p["auto_fulfill"] and p["supplier_product_id"]:
            stock_info = f"⚡Auto ({p['supplier_product_id']})"
        else:
            stock_info = f"stock {stock_count(p['id'])}"

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
            f"{p['id']}. {esc(p['name'])}{cat_disp} · {usd(p['price'])}{cost_disp}{margin_info} · {stock_info} · "
            f"{'on' if p['active'] else 'off'}"
        )
    await m.answer("\n".join(lines))


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
# Supplier Admin Commands
# --------------------------------------------------------------------------
@dp.message(Command("supplier"), is_admin)
async def admin_supplier_info(m: Message):
    if not RESELLER_API_KEY:
        return await m.answer("⚠️ <code>RESELLER_API_KEY</code> is not configured in .env.")
    try:
        bal_res = await reseller.get_balance()
        bal_str = f"${bal_res.get('balance', 0):.2f} {bal_res.get('currency', 'USD')}"
    except Exception as e:
        bal_str = f"Error: {e}"

    try:
        info_res = await reseller.get_info()
        info_text = json.dumps(info_res, indent=2, ensure_ascii=False)
        if len(info_text) > 400:
            info_text = info_text[:400] + "..."
    except Exception as e:
        info_text = f"Error: {e}"

    await m.answer(
        f"🌐 <b>Supplier API Status</b>\n"
        f"<b>Base URL:</b> <code>{esc(RESELLER_BASE_URL)}</code>\n"
        f"<b>API Key:</b> {'Set (✓)' if RESELLER_API_KEY else 'Missing (✗)'}\n"
        f"<b>Supplier Balance:</b> <b>{esc(bal_str)}</b>\n\n"
        f"<b>Account Info:</b>\n<pre>{esc(info_text)}</pre>"
    )


@dp.message(Command("s_products"), is_admin)
async def admin_supplier_products(m: Message):
    if not RESELLER_API_KEY:
        return await m.answer("⚠️ <code>RESELLER_API_KEY</code> is not configured in .env.")
    try:
        products = await reseller.get_products()
    except Exception as e:
        return await m.answer(f"Failed to fetch supplier catalog: {esc(str(e))}")

    if not products:
        return await m.answer("No products found in supplier catalog.")

    local_map = {row["supplier_product_id"]: row for row in db.execute(
        "SELECT id, name, price, supplier_product_id FROM products WHERE supplier_product_id IS NOT NULL").fetchall()}

    lines = ["<b>Supplier Catalog Products:</b>\n"]
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


@dp.message(Command("s_sync"), is_admin)
async def admin_supplier_sync(m: Message, command: CommandObject):
    if not RESELLER_API_KEY:
        return await m.answer("⚠️ <code>RESELLER_API_KEY</code> is not configured in .env.")

    markup = PROFIT_MARGIN
    if command.args:
        custom_markup = parse_profit_margin(command.args.strip(), default=markup)
        if custom_markup >= 0:
            markup = custom_markup

    try:
        sup_products = await reseller.get_products()
    except Exception as e:
        return await m.answer(f"Failed to fetch supplier catalog: {esc(str(e))}")

    if not sup_products:
        return await m.answer("No products returned by supplier.")

    added, updated = 0, 0
    for sp in sup_products:
        sp_id = sp["id"]
        cost_cents = sp["price_cents"]
        cat = detect_category(sp["name"])

        existing = db.execute("SELECT id, margin FROM products WHERE supplier_product_id=?", (sp_id,)).fetchone()
        item_margin = existing["margin"] if (existing and existing["margin"] is not None) else markup
        retail_cents = max(100, round(cost_cents * (1 + item_margin / 100)))

        if existing:
            db.execute("""
                UPDATE products
                SET name=?, description=?, price=?, supplier_price=?, auto_fulfill=1, active=1, category=?
                WHERE id=?
            """, (sp["name"], sp["description"], retail_cents, cost_cents, cat, existing["id"]))
            updated += 1
        else:
            db.execute("""
                INSERT INTO products(name, description, price, supplier_product_id, supplier_price, auto_fulfill, active, category)
                VALUES(?,?,?,?,?,1,1,?)
            """, (sp["name"], sp["description"], retail_cents, sp_id, cost_cents, cat))
            added += 1

    await m.answer(
        f"✅ <b>Catalog Sync Complete!</b>\n"
        f"• Profit margin markup: <b>{markup}%</b>\n"
        f"• New products imported: <b>{added}</b>\n"
        f"• Existing products updated: <b>{updated}</b>\n"
        f"• Total catalog items: <b>{len(sup_products)}</b>"
    )


@dp.message(Command("s_link"), is_admin)
async def admin_supplier_link(m: Message, command: CommandObject):
    try:
        local_id, sup_id = command.args.split()
        local_id = int(local_id)
        p = db.execute("SELECT * FROM products WHERE id=?", (local_id,)).fetchone()
        if not p:
            return await m.answer(f"Product #{local_id} not found.")
        db.execute("UPDATE products SET supplier_product_id=?, auto_fulfill=1 WHERE id=?", (sup_id.strip(), local_id))
        await m.answer(f"Product #{local_id} ({esc(p['name'])}) linked to supplier product <code>{esc(sup_id)}</code>.")
    except Exception:
        await m.answer("Usage: /s_link <local_product_id> <supplier_product_id>")


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

    try:
        await bot.delete_webhook(drop_pending_updates=True)
        await bot.set_my_commands([
            BotCommand(command="start", description="Main menu"),
            BotCommand(command="orders", description="View recent purchases"),
        ])
        await dp.start_polling(bot)
    finally:
        await reseller.close()
        await runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
