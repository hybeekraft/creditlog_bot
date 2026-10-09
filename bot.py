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
import hashlib
import hmac
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

# Payment Gateway (NOWPayments)
NOW_API_KEY = os.environ.get("NOWPAYMENTS_API_KEY", "")
NOW_IPN_SECRET = os.environ.get("NOWPAYMENTS_IPN_SECRET", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")  # https://shop.example.com

# Reseller / Supplier Catalog API
RESELLER_BASE_URL = os.environ.get("RESELLER_BASE_URL", "https://api-geminipro.ignorelist.com/api/reseller/v1").rstrip("/")
RESELLER_API_KEY = os.environ.get("RESELLER_API_KEY", "")
RESELLER_MARKUP_PERCENT = int(os.environ.get("RESELLER_MARKUP_PERCENT", "20"))
reseller = ResellerClient(base_url=RESELLER_BASE_URL, api_key=RESELLER_API_KEY)

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
  auto_fulfill INTEGER DEFAULT 0);
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
  payload TEXT,
  refunded INTEGER DEFAULT 0,
  supplier_order_id TEXT DEFAULT NULL,
  status TEXT DEFAULT 'completed',
  created TEXT DEFAULT CURRENT_TIMESTAMP);
""")

def migrate_db():
    cols_p = {row["name"] for row in db.execute("PRAGMA table_info(products)").fetchall()}
    if "supplier_product_id" not in cols_p:
        db.execute("ALTER TABLE products ADD COLUMN supplier_product_id TEXT DEFAULT NULL")
    if "supplier_price" not in cols_p:
        db.execute("ALTER TABLE products ADD COLUMN supplier_price INTEGER DEFAULT NULL")
    if "auto_fulfill" not in cols_p:
        db.execute("ALTER TABLE products ADD COLUMN auto_fulfill INTEGER DEFAULT 0")

    cols_o = {row["name"] for row in db.execute("PRAGMA table_info(orders)").fetchall()}
    if "supplier_order_id" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN supplier_order_id TEXT DEFAULT NULL")
    if "status" not in cols_o:
        db.execute("ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'completed'")

migrate_db()

dp = Dispatcher()
usd = lambda c: f"${c / 100:.2f}"
esc = html.escape

# --------------------------------------------------------------------------
# Translations
# --------------------------------------------------------------------------
TR = {
    "en": {
        "welcome": "🛍 <b>Welcome to the Store</b>\n\n💵 Best prices\n⚡ Instant delivery after checkout\n🆕 Digital goods catalog\n💬 Need help? Contact support\n\nChoose an option below:",
        "btn_shop": "🛍 Browse Catalog",
        "btn_orders": "📦 Recent Purchases",
        "btn_lang": "🌐 Language",
        "btn_support": "💬 Support",
        "btn_news": "📢 News Channel",
        "btn_back": "⬅ Back",
        "btn_buy": "💳 Buy / Checkout",
        "btn_pay": "💳 Pay with Crypto ({amount})",
        "shop_title": "<b>Available Products</b>",
        "no_products": "No products available at the moment.",
        "product": "<b>{name}</b>\n{desc}\n\nPrice: <b>{price}</b>\nAvailability: {stock}",
        "out_of_stock": "Out of stock or unavailable.",
        "checkout_invoice": "💳 <b>Order #{oid} - Checkout</b>\n\nItem: <b>{name}</b>\nTotal: <b>{amount}</b>\n\nClick the button below to complete payment. Your digital code/link will be delivered automatically upon payment.",
        "purchased": "✅ <b>Purchase Complete!</b>\nOrder #{oid}\n\n<code>{payload}</code>",
        "orders_title": "📦 <b>Your Recent Purchases</b>",
        "no_orders": "You haven't made any purchases yet. Browse the catalog to get started!",
        "lang_choose": "Choose your language:",
        "lang_set": "Language updated.",
        "stock_auto": "⚡ Instant Auto-Delivery",
        "supplier_error": "⚠️ Product is temporarily unavailable from supplier. Please try again shortly or contact support.",
        "checkout_unavailable": "Checkout is temporarily unavailable. Please contact support.",
    },
    "es": {
        "welcome": "🛍 <b>Bienvenido a la Tienda</b>\n\n💵 Mejores precios\n⚡ Entrega instantánea tras el pago\n🆕 Catálogo de productos digitales\n💬 ¿Necesitas ayuda? Contacta a soporte\n\nElige una opción:",
        "btn_shop": "🛍 Explorar Catálogo",
        "btn_orders": "📦 Compras Recientes",
        "btn_lang": "🌐 Idioma",
        "btn_support": "💬 Soporte",
        "btn_news": "📢 Canal de Noticias",
        "btn_back": "⬅ Volver",
        "btn_buy": "💳 Comprar / Pagar",
        "btn_pay": "💳 Pagar con Crypto ({amount})",
        "shop_title": "<b>Productos Disponibles</b>",
        "no_products": "No hay productos disponibles por ahora.",
        "product": "<b>{name}</b>\n{desc}\n\nPrecio: <b>{price}</b>\nDisponibilidad: {stock}",
        "out_of_stock": "Agotado o no disponible.",
        "checkout_invoice": "💳 <b>Pedido #{oid} - Pago</b>\n\nArtículo: <b>{name}</b>\nTotal: <b>{amount}</b>\n\nPresiona el botón de abajo para pagar. Tu código/enlace digital se entregará automáticamente tras la confirmación.",
        "purchased": "✅ <b>¡Compra Completada!</b>\nPedido #{oid}\n\n<code>{payload}</code>",
        "orders_title": "📦 <b>Tus Compras Recientes</b>",
        "no_orders": "Aún no has realizado compras. ¡Explora el catálogo para comenzar!",
        "lang_choose": "Elige tu idioma:",
        "lang_set": "Idioma actualizado.",
        "stock_auto": "⚡ Entrega Automática Instantánea",
        "supplier_error": "⚠️ El producto no está disponible temporalmente en el proveedor. Intenta más tarde o contacta a soporte.",
        "checkout_unavailable": "El pago no está disponible temporalmente. Contacta a soporte.",
    },
    "ru": {
        "welcome": "🛍 <b>Добро пожаловать в магазин</b>\n\n💵 Лучшие цены\n⚡ Мгновенная выдача после оплаты\n🆕 Каталог цифровых товаров\n💬 Нужна помощь? Напишите в поддержку\n\nВыберите раздел:",
        "btn_shop": "🛍 Каталог товаров",
        "btn_orders": "📦 Мои покупки",
        "btn_lang": "🌐 Язык",
        "btn_support": "💬 Поддержка",
        "btn_news": "📢 Новости",
        "btn_back": "⬅ Назад",
        "btn_buy": "💳 Купить / Оформить",
        "btn_pay": "💳 Оплатить криптовалютой ({amount})",
        "shop_title": "<b>Доступные товары</b>",
        "no_products": "Товаров пока нет в наличии.",
        "product": "<b>{name}</b>\n{desc}\n\nЦена: <b>{price}</b>\nНаличие: {stock}",
        "out_of_stock": "Нет в наличии.",
        "checkout_invoice": "💳 <b>Заказ №{oid} - Оплата</b>\n\nТовар: <b>{name}</b>\nК оплате: <b>{amount}</b>\n\nНажмите кнопку ниже для оплаты. Товар будет отправлен мгновенно после подтверждения платежа.",
        "purchased": "✅ <b>Покупка завершена!</b>\nЗаказ №{oid}\n\n<code>{payload}</code>",
        "orders_title": "📦 <b>Ваши недавние покупки</b>",
        "no_orders": "У вас пока нет покупок. Перейдите в каталог, чтобы сделать заказ!",
        "lang_choose": "Выберите язык:",
        "lang_set": "Язык обновлён.",
        "stock_auto": "⚡ Мгновенная автовыдача",
        "supplier_error": "⚠️ Товар временно недоступен у поставщика. Пожалуйста, попробуйте позже или напишите в поддержку.",
        "checkout_unavailable": "Оплата временно недоступна. Напишите в поддержку.",
    },
    "ar": {
        "welcome": "🛍 <b>مرحبًا بك في المتجر</b>\n\n💵 أفضل الأسعار\n⚡ تسليم فوري بعد الدفع\n🆕 كتالوج السلع الرقمية\n💬 هل تحتاج مساعدة؟ تواصل مع الدعم\n\nاختر من القائمة أدناه:",
        "btn_shop": "🛍 تصفح الكتالوج",
        "btn_orders": "📦 مشترياتي الأخيرة",
        "btn_lang": "🌐 اللغة",
        "btn_support": "💬 الدعم",
        "btn_news": "📢 قناة الأخبار",
        "btn_back": "⬅ رجوع",
        "btn_buy": "💳 شراء / دفع",
        "btn_pay": "💳 الدفع بالعملات الرقمية ({amount})",
        "shop_title": "<b>المنتجات المتوفرة</b>",
        "no_products": "لا توجد منتجات متوفرة حاليًا.",
        "product": "<b>{name}</b>\n{desc}\n\nالسعر: <b>{price}</b>\nالحالة: {stock}",
        "out_of_stock": "نفدت الكمية أو غير متوفر.",
        "checkout_invoice": "💳 <b>الطلب #{oid} - إتمام الدفع</b>\n\nالمنتج: <b>{name}</b>\nالمجموع: <b>{amount}</b>\n\nاضغط على الزر أدناه للدفع. سيتم تسليم المنتج الرقمي فورًا بعد التأكيد.",
        "purchased": "✅ <b>تمت عملية الشراء بنجاح!</b>\nالطلب #{oid}\n\n<code>{payload}</code>",
        "orders_title": "📦 <b>مشترياتك الأخيرة</b>",
        "no_orders": "ليس لديك أي مشتريات حتى الآن. تصفح الكتالوج للبدء!",
        "lang_choose": "اختر لغتك:",
        "lang_set": "تم تحديث اللغة.",
        "stock_auto": "⚡ تسليم فوري وتلقائي",
        "supplier_error": "⚠️ المنتج غير متوفر مؤقتًا لدى المورد. يرجى المحاولة لاحقًا أو التواصل مع الدعم.",
        "checkout_unavailable": "الدفع غير متاح مؤقتًا. يرجى التواصل مع الدعم.",
    },
}
LANG_NAMES = {"en": "English", "es": "Español", "ru": "Русский", "ar": "العربية"}

# --------------------------------------------------------------------------
# Helpers
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
    return TR.get(lang, TR["en"]).get(key, TR["en"][key]).format(**kw)


def kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn(text, data=None, url=None):
    return InlineKeyboardButton(text=text, callback_data=data, url=url)


def stock_count(pid: int) -> int:
    return db.execute("SELECT COUNT(*) FROM stock WHERE product_id=? AND sold=0", (pid,)).fetchone()[0]


def stock_display(p: sqlite3.Row, uid: int) -> str:
    if p["auto_fulfill"] and p["supplier_product_id"]:
        return t(uid, "stock_auto")
    return str(stock_count(p["id"]))


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
    # Quick product highlights
    rows = [[btn(f"{p['name']} · {usd(p['price'])}", f"p:{p['id']}")]
            for p in db.execute("SELECT * FROM products WHERE active=1 ORDER BY id LIMIT 5")]
    rows += [
        [btn(t(uid, "btn_shop"), "shop"), btn(t(uid, "btn_orders"), "orders")],
        [btn(t(uid, "btn_lang"), "lang"), btn(t(uid, "btn_support"), url=SUPPORT_URL)],
        [btn(t(uid, "btn_news"), url=CHANNEL_URL)],
    ]
    return kb(rows)


# --------------------------------------------------------------------------
# Order Fulfillment Logic
# --------------------------------------------------------------------------
async def fulfill_order(bot: Bot, oid: int) -> Tuple[Optional[str], Optional[str]]:
    """
    Fulfills an order automatically:
      - If supplier catalog item: calls Reseller API POST /orders, saves order.value.
      - If manual stock: pulls available stock item from database.
    Returns (payload, error_key).
    """
    o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
    if not o:
        return None, "out_of_stock"
    if o["payload"] and o["status"] == "completed":
        return o["payload"], None

    p = db.execute("SELECT * FROM products WHERE id=?", (o["product_id"],)).fetchone()
    if not p:
        return None, "out_of_stock"

    # Option A: Supplier Catalog Auto-fulfillment
    if p["auto_fulfill"] and p["supplier_product_id"]:
        try:
            order_res = await reseller.create_order(product_id=p["supplier_product_id"], quantity=1)
            payload = order_res.get("value")
            supplier_order_id = order_res.get("order_code")
            if not payload:
                raise ResellerAPIError(f"Supplier order succeeded but returned empty value: {order_res}")
        except Exception as e:
            log.exception("Supplier order failed for order %s", oid)
            db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
            await notify_admins(bot, f"⚠️ Supplier order failed for order #{oid} (user {o['user_id']}, product #{p['id']}):\n<code>{esc(str(e))}</code>")
            return None, "supplier_error"

        db.execute(
            "UPDATE orders SET payload=?, supplier_order_id=?, status='completed' WHERE id=?",
            (payload, supplier_order_id, oid)
        )
        return payload, None

    # Option B: Local Stock Item
    db.execute("BEGIN IMMEDIATE")
    try:
        s = db.execute("SELECT * FROM stock WHERE product_id=? AND sold=0 ORDER BY id LIMIT 1", (p["id"],)).fetchone()
        if not s:
            db.execute("ROLLBACK")
            db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
            return None, "out_of_stock"
        db.execute("UPDATE stock SET sold=1 WHERE id=?", (s["id"],))
        db.execute("UPDATE orders SET payload=?, status='completed' WHERE id=?", (s["payload"], oid))
        db.execute("COMMIT")
        return s["payload"], None
    except Exception:
        db.execute("ROLLBACK")
        raise


# --------------------------------------------------------------------------
# User handlers (Guest Flow)
# --------------------------------------------------------------------------
@dp.message(CommandStart())
async def start(m: Message):
    get_user(m.from_user.id, m.from_user.language_code)
    await m.answer(t(m.from_user.id, "welcome"), reply_markup=home_kb(m.from_user.id))


@dp.callback_query(F.data == "home")
async def home(c: CallbackQuery):
    uid = c.from_user.id
    await edit(c, t(uid, "welcome"), home_kb(uid))


@dp.callback_query(F.data == "shop")
async def shop(c: CallbackQuery):
    uid = c.from_user.id
    prods = db.execute("SELECT * FROM products WHERE active=1 ORDER BY id").fetchall()
    if not prods:
        return await edit(c, t(uid, "no_products"), kb([[btn(t(uid, "btn_back"), "home")]]))
    rows = []
    for p in prods:
        indicator = "⚡ Auto" if (p["auto_fulfill"] and p["supplier_product_id"]) else stock_count(p["id"])
        rows.append([btn(f"{p['name']} · {usd(p['price'])} ({indicator})", f"p:{p['id']}")])
    rows.append([btn(t(uid, "btn_back"), "home")])
    await edit(c, t(uid, "shop_title"), kb(rows))


@dp.callback_query(F.data.startswith("p:"))
async def product(c: CallbackQuery):
    uid = c.from_user.id
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (int(c.data[2:]),)).fetchone()
    if not p:
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)
    text = t(uid, "product", name=esc(p["name"]), desc=esc(p["description"] or ""),
             price=usd(p["price"]), stock=stock_display(p, uid))
    await edit(c, text, kb([[btn(t(uid, "btn_buy"), f"buy:{p['id']}")],
                            [btn(t(uid, "btn_back"), "shop")]]))


async def create_order_invoice(order_id: int, item_name: str, cents: int) -> str:
    """Create a NOWPayments direct invoice for this specific product order."""
    payload = {
        "price_amount": cents / 100,
        "price_currency": "usd",
        "order_id": f"O{order_id}",
        "order_description": f"Order #{order_id}: {item_name}",
        "ipn_callback_url": f"{PUBLIC_URL}/ipn/nowpayments",
    }
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(timeout=timeout) as s:
        async with s.post("https://api.nowpayments.io/v1/invoice", json=payload,
                          headers={"x-api-key": NOW_API_KEY}) as r:
            data = await r.json()
            if r.status >= 400:
                raise RuntimeError(f"NOWPayments error {r.status}: {data}")
            return data["invoice_url"]


@dp.callback_query(F.data.startswith("buy:"))
async def buy(c: CallbackQuery):
    """Guest checkout for a single product."""
    uid, pid = c.from_user.id, int(c.data[4:])
    p = db.execute("SELECT * FROM products WHERE id=? AND active=1", (pid,)).fetchone()
    if not p:
        return await c.answer(t(uid, "out_of_stock"), show_alert=True)

    # Check local stock availability if not auto-fulfillment
    if not (p["auto_fulfill"] and p["supplier_product_id"]):
        if stock_count(pid) <= 0:
            return await c.answer(t(uid, "out_of_stock"), show_alert=True)

    # Mode 1: Payment gateway (NOWPayments) enabled -> generate direct checkout link
    if NOW_API_KEY and NOW_IPN_SECRET and PUBLIC_URL:
        cur = db.execute(
            "INSERT INTO orders(user_id, product_id, product_name, price, status) VALUES(?,?,?,?,'pending')",
            (uid, pid, p["name"], p["price"])
        )
        oid = cur.lastrowid
        try:
            url = await create_order_invoice(oid, p["name"], p["price"])
        except Exception:
            log.exception("Invoice creation failed")
            db.execute("UPDATE orders SET status='failed' WHERE id=?", (oid,))
            return await c.answer(t(uid, "checkout_unavailable"), show_alert=True)

        await edit(c, t(uid, "checkout_invoice", oid=oid, name=esc(p["name"]), amount=usd(p["price"])),
                   kb([[btn(t(uid, "btn_pay", amount=usd(p["price"])), url=url)],
                       [btn(t(uid, "btn_back"), f"p:{pid}")]]))
        return

    # Mode 2: Direct fulfillment mode (testing or free instant checkout)
    cur = db.execute(
        "INSERT INTO orders(user_id, product_id, product_name, price, status) VALUES(?,?,?,?,'processing')",
        (uid, pid, p["name"], p["price"])
    )
    oid = cur.lastrowid
    payload, err = await fulfill_order(c.bot, oid)
    if err:
        return await c.answer(t(uid, err), show_alert=True)

    await c.message.answer(t(uid, "purchased", oid=oid, payload=esc(payload)))
    await c.answer()

    if not p["auto_fulfill"]:
        left = stock_count(pid)
        if left <= LOW_STOCK:
            name = db.execute("SELECT name FROM products WHERE id=?", (pid,)).fetchone()["name"]
            await notify_admins(c.bot, f"⚠️ Low stock: <b>{esc(name)}</b> (id {pid}) has {left} left.")


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
# IPN Webhook (NOWPayments)
# --------------------------------------------------------------------------
def verify_sig(body: dict, sig: str) -> bool:
    msg = json.dumps(body, separators=(",", ":"), sort_keys=True)
    calc = hmac.new(NOW_IPN_SECRET.encode(), msg.encode(), hashlib.sha512).hexdigest()
    return hmac.compare_digest(calc, sig or "")


async def ipn_handler(request: web.Request):
    bot: Bot = request.app["bot"]
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400)
    if not NOW_IPN_SECRET or not verify_sig(data, request.headers.get("x-nowpayments-sig", "")):
        log.warning("IPN with bad signature rejected")
        return web.Response(status=403)

    order_id_str, status = str(data.get("order_id", "")), data.get("payment_status")
    if order_id_str.startswith("O") and order_id_str[1:].isdigit():
        oid = int(order_id_str[1:])
        if status == "finished":
            o = db.execute("SELECT * FROM orders WHERE id=?", (oid,)).fetchone()
            if o and o["status"] != "completed":
                payload, err = await fulfill_order(bot, oid)
                if payload:
                    try:
                        await bot.send_message(o["user_id"], t(o["user_id"], "purchased", oid=oid, payload=esc(payload)))
                    except Exception:
                        pass
                    await notify_admins(bot, f"✅ Order #{oid} paid & delivered: {esc(o['product_name'])} ({usd(o['price'])}) to user {o['user_id']}")
                else:
                    await notify_admins(bot, f"⚠️ Order #{oid} paid by user {o['user_id']} but fulfillment failed ({err})! Needs manual delivery.")
        elif status in ("failed", "expired", "refunded"):
            db.execute("UPDATE orders SET status=? WHERE id=? AND status='pending'", (status, oid))

    return web.Response(text="ok")


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
        "/products - List all products\n"
        "/addproduct &lt;price_usd&gt; &lt;name&gt; [| description]\n"
        "/setprice &lt;product_id&gt; &lt;price_usd&gt;\n"
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
        lines.append(
            f"{p['id']}. {esc(p['name'])} · {usd(p['price'])} · {stock_info} · "
            f"{'on' if p['active'] else 'off'}"
        )
    await m.answer("\n".join(lines))


@dp.message(Command("addproduct"), is_admin)
async def admin_addproduct(m: Message, command: CommandObject):
    try:
        price, rest = command.args.split(None, 1)
        name, _, desc = rest.partition("|")
        pid = db.execute("INSERT INTO products(name, description, price) VALUES(?,?,?)",
                         (name.strip(), desc.strip(), round(float(price) * 100))).lastrowid
        await m.answer(f"Product #{pid} added.")
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

    markup = RESELLER_MARKUP_PERCENT
    if command.args and command.args.strip().isdigit():
        markup = int(command.args.strip())

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
        retail_cents = max(100, round(cost_cents * (1 + markup / 100)))

        existing = db.execute("SELECT id FROM products WHERE supplier_product_id=?", (sp_id,)).fetchone()
        if existing:
            db.execute("""
                UPDATE products
                SET name=?, description=?, price=?, supplier_price=?, auto_fulfill=1, active=1
                WHERE id=?
            """, (sp["name"], sp["description"], retail_cents, cost_cents, existing["id"]))
            updated += 1
        else:
            db.execute("""
                INSERT INTO products(name, description, price, supplier_product_id, supplier_price, auto_fulfill, active)
                VALUES(?,?,?,?,?,1,1)
            """, (sp["name"], sp["description"], retail_cents, sp_id, cost_cents))
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
    app["bot"] = bot
    app.router.add_post("/ipn/nowpayments", ipn_handler)
    app.router.add_get("/health", lambda r: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("IPN server listening on :%s", PORT)

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
