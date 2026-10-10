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

from reseller import ResellerClient, ResellerAPIError, SupplierRegistry

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
if not CRYPTO_USDT_BEP20 or CRYPTO_USDT_BEP20.startswith("0xYour"):
    CRYPTO_USDT_BEP20 = "0x4B20993Bc481177ec7E8f571ceCaE8A9e22C02db"

CRYPTO_USDT_TRC20 = (os.environ.get("CRYPTO_USDT_TRC20", "") or "").strip()
if not CRYPTO_USDT_TRC20 or CRYPTO_USDT_TRC20.startswith("TYour"):
    CRYPTO_USDT_TRC20 = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

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
  supplier TEXT DEFAULT 'default');
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
    if "supplier" not in cols_p:
        db.execute("ALTER TABLE products ADD COLUMN supplier TEXT DEFAULT 'default'")

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

    # Update categories for existing products if not assigned
    for row in db.execute("SELECT id, name, category FROM products").fetchall():
        if not row["category"] or row["category"] == "AI Tools":
            detected = detect_category(row["name"])
            if detected != "AI Tools":
                db.execute("UPDATE products SET category=? WHERE id=?", (detected, row["id"]))

    # Ensure default showcase products (Gemini 18 Months and Duolingo Super 12 Months)
    g = db.execute("SELECT id FROM products WHERE name LIKE '%Gemini 18 Months%'").fetchone()
    if not g:
        db.execute("""
            INSERT INTO products(name, description, price, category, auto_fulfill, active)
            VALUES('Gemini 18 Months', 'Gemini Advanced & Pro 18 Months Subscription', 72, 'Gemini', 1, 1)
        """)
    d = db.execute("SELECT id FROM products WHERE name LIKE '%Duolingo%'").fetchone()
    if not d:
        db.execute("""
            INSERT INTO products(name, description, price, category, auto_fulfill, active)
            VALUES('Duolingo Super 12 Months', 'Duolingo Super 12 Months Plan', 90, 'Education', 1, 1)
        """)

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


PAGE_SIZE = 12


def render_product_list(uid: int, prods: list, page: int, cb_prefix: str, ctx_prefix: str, back_cb: str) -> Tuple[str, InlineKeyboardMarkup]:
    """1-column product list matching Screenshot 4."""
    total_pages = max(1, (len(prods) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, total_pages))

    if not prods:
        return t(uid, "no_products"), kb([[btn(t(uid, "btn_back"), back_cb, style="danger")]])

    slice_prods = prods[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    rows = []
    for p in slice_prods:
        p_cat = p["category"] if "category" in p.keys() else None
        cat_key = CAT_KEY_BY_NAME.get((p_cat or "").lower()) or CAT_KEY_BY_NAME.get(detect_category(p["name"]).lower())
        p_emoji_id = get_custom_emoji_id(cat_key) if cat_key else None
        icon = get_product_icon(p["name"], p_cat)
        btn_text = f"{p['name']} · {usd(p['price'])} ✅" if p_emoji_id else f"{icon} {p['name']} · {usd(p['price'])} ✅"
        rows.append([btn(btn_text, f"p:{p['id']}:{ctx_prefix}_{page}", style="success", emoji_id=p_emoji_id)])

    if total_pages > 1:
        pag_row = []
        if page > 1:
            pag_row.append(btn("◀️", f"{cb_prefix}:{page - 1}", style="primary"))
        pag_row.append(btn(f"{page}/{total_pages}", "noop", style="primary"))
        if page < total_pages:
            pag_row.append(btn("▶️", f"{cb_prefix}:{page + 1}", style="primary"))
        rows.append(pag_row)

    rows.append([
        btn(t(uid, "btn_refresh"), f"{cb_prefix}:{page}", style="primary"),
        btn(t(uid, "btn_back"), back_cb, style="danger")
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

    # 2. Binance Pay (Screenshot 4)
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
            f"⌛ <b>Step 1:</b> Open Binance App → Pay → Send to Binance Pay ID: <code>{BINANCE_PAY_ID}</code>.\n"
            "🔗 <b>Step 2:</b> Copy your <b>Binance Pay Order ID / Transaction ID</b>.\n"
            "✅ <b>Step 3:</b> Tap <b>Submit Transaction ID</b> below and paste it in the chat.\n\n"
            "⏳ <b>Please pay within 70 minutes</b>, otherwise this order expires and needs manual review.\n\n"
            "<i>Delivery is instant once verified by admin.</i>"
        )
        rows = [
            [btn("🔗 Submit Transaction ID", f"submit_tx:{oid}", style="primary")],
            [btn("✅ I've Paid — Check Status", f"chk_manual:{oid}", style="success")],
            [btn("◀️ Back", f"pay_dir:{pid}:{qty}:{ctx}", style="primary")],
            [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
        ]
        return await edit(c, text, kb(rows))

    # 3. USDT BEP20
    if method == "bep20":
        text = (
            "<u>🟠 <b>USDT BEP20</b></u>\n"
            "────────────────────────\n"
            f"🥇 <b>Product:</b> {icon} <b>{esc(p['name'])}</b>\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            "➡️ <b>Send exactly this amount:</b>\n"
            f"<code>{total_usdt} USDT</code>\n"
            "👆 <i>Tap to copy.</i>\n"
            "────────────────────────\n"
            "🏦 <b>Network / Address (BEP20 / BSC):</b>\n"
            f"<code>{CRYPTO_USDT_BEP20}</code>\n"
            "👆 <i>Tap to copy — always copy it from this message.</i>\n\n"
            "⚠️ <b>Send only USDT via BNB Smart Chain (BEP20) to the address above.</b>\n"
            "────────────────────────\n"
            f"⌛ <b>Step 1:</b> Send <code>{total_usdt} USDT</code> via BEP20 to the address above.\n"
            "🔗 <b>Step 2:</b> Copy your <b>Transaction Hash / TxID</b>.\n"
            "✅ <b>Step 3:</b> Tap <b>Submit Transaction ID</b> below and paste it in the chat.\n\n"
            "⏳ <b>Please pay within 70 minutes</b>, otherwise this order expires and needs manual review.\n\n"
            "<i>Delivery is instant once verified by admin.</i>"
        )
        rows = [
            [btn("🔗 Submit Transaction ID", f"submit_tx:{oid}", style="primary")],
            [btn("✅ I've Paid — Check Status", f"chk_manual:{oid}", style="success")],
            [btn("◀️ Back", f"pay_dir:{pid}:{qty}:{ctx}", style="primary")],
            [btn("❌ Cancel Order", f"cancel_order:{oid}", style="danger")]
        ]
        return await edit(c, text, kb(rows))

    # 4. USDT TRC20
    if method == "trc20":
        text = (
            "<u>🔴 <b>USDT TRC20</b></u>\n"
            "────────────────────────\n"
            f"🥇 <b>Product:</b> {icon} <b>{esc(p['name'])}</b>\n"
            f"🔢 <b>Quantity:</b> {qty}\n"
            "➡️ <b>Send exactly this amount:</b>\n"
            f"<code>{total_usdt} USDT</code>\n"
            "👆 <i>Tap to copy.</i>\n"
            "────────────────────────\n"
            "🏦 <b>Network / Address (TRC20 / TRON):</b>\n"
            f"<code>{CRYPTO_USDT_TRC20}</code>\n"
            "👆 <i>Tap to copy — always copy it from this message.</i>\n\n"
            "⚠️ <b>Send only USDT via TRON (TRC20) to the address above.</b>\n"
            "────────────────────────\n"
            f"⌛ <b>Step 1:</b> Send <code>{total_usdt} USDT</code> via TRC20 to the address above.\n"
            "🔗 <b>Step 2:</b> Copy your <b>Transaction Hash / TxID</b>.\n"
            "✅ <b>Step 3:</b> Tap <b>Submit Transaction ID</b> below and paste it in the chat.\n\n"
            "⏳ <b>Please pay within 70 minutes</b>, otherwise this order expires and needs manual review.\n\n"
            "<i>Delivery is instant once verified by admin.</i>"
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
        return await c.answer("⚠️ Please tap 'Submit Transaction ID' first so admins can verify your payment.", show_alert=True)
    else:
        return await c.answer("⏳ Your payment is currently under verification by admin. You will receive your item automatically once confirmed!", show_alert=True)


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

    elif action == "txid":
        oid = state["oid"]
        txid = m.text.strip()
        o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
        if not o:
            return await m.answer("⚠️ Order not found.")
        db.execute("UPDATE orders SET txid=? WHERE id=?", (txid, oid))
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
    await edit(c, text, kb([[btn(t(uid, "btn_support"), url=SUPPORT_URL, style="primary")],
                            [btn(t(uid, "btn_back"), f"p:{o['product_id']}", style="danger")] ]))


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

    markup = kb([[btn(t(uid, "btn_back"), "home", style="danger")]])
    if isinstance(event, CallbackQuery):
        await edit(event, text, markup)
    else:
        await event.answer(text, reply_markup=markup)


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

        for sp in sup_products:
            sp_id = sp["id"]
            cost_cents = sp["price_cents"]
            cat = detect_category(sp["name"])

            existing = db.execute(
                "SELECT id, margin FROM products WHERE supplier_product_id=? AND supplier=?",
                (sp_id, sup_name)
            ).fetchone()
            item_margin = existing["margin"] if (existing and existing["margin"] is not None) else markup
            retail_cents = max(100, round(cost_cents * (1 + item_margin / 100)))

            if existing:
                db.execute("""
                    UPDATE products
                    SET name=?, description=?, price=?, supplier_price=?, auto_fulfill=1, active=1, category=?, supplier=?
                    WHERE id=?
                """, (sp["name"], sp["description"], retail_cents, cost_cents, cat, sup_name, existing["id"]))
                total_updated += 1
            else:
                db.execute("""
                    INSERT INTO products(name, description, price, supplier_product_id, supplier_price, auto_fulfill, active, category, supplier)
                    VALUES(?,?,?,?,?,1,1,?,?)
                """, (sp["name"], sp["description"], retail_cents, sp_id, cost_cents, cat, sup_name))
                total_added += 1

        total_items += len(sup_products)

    res_msg = (
        f"✅ <b>Catalog Sync Complete!</b>\n"
        f"• Suppliers synced: <b>{', '.join(target_suppliers)}</b>\n"
        f"• Default profit margin: <b>{markup}%</b>\n"
        f"• New products imported: <b>{total_added}</b>\n"
        f"• Existing products updated: <b>{total_updated}</b>\n"
        f"• Total catalog items processed: <b>{total_items}</b>"
    )
    if errors:
        res_msg += "\n\n⚠️ <b>Errors:</b>\n" + "\n".join(f"• {esc(err)}" for err in errors)

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
