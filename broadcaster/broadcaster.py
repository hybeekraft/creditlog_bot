import os
import sys
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Load local broadcaster .env first, then root creditlogbot .env
load_dotenv(BASE_DIR / ".env")
load_dotenv(ROOT_DIR / ".env")

try:
    from telethon import TelegramClient, events, Button
    from telethon.sessions import StringSession
except ImportError:
    TelegramClient = None
    events = None
    Button = None
    StringSession = None

try:
    from .parser import MessageParser
    from .pricing import PricingEngine
except ImportError:
    from parser import MessageParser
    from pricing import PricingEngine

# Configuration
API_ID = os.getenv("TELEGRAM_API_ID", "").strip().strip('"\'')
API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip().strip('"\'')
STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "").strip().strip('"\'')
SESSION_NAME = os.getenv("SESSION_NAME", "stock_broadcaster").strip()

# Source channels/bots to listen to (comma-separated, e.g. "@supplier_channel,@supplier_bot")
SOURCE_CHATS_RAW = os.getenv("SOURCE_CHATS", "")

# Destination channel: defaults to root .env CHANNEL_URL (e.g. https://t.me/credit_log -> @credit_log)
raw_dest = os.getenv("DESTINATION_CHANNEL", "")
if not raw_dest:
    channel_url = os.getenv("CHANNEL_URL", "")
    if channel_url:
        raw_dest = "@" + channel_url.rstrip("/").split("/")[-1].lstrip("@")
DESTINATION_CHANNEL = raw_dest

MY_BOT_USERNAME = (
    os.getenv("MY_BOT_USERNAME")
    or os.getenv("BOT_USERNAME")
    or "creditlogbot"
).strip().lstrip("@")
if MY_BOT_USERNAME.lower() in ("creditlog_bot", "credilog_bot", "creditlog"):
    MY_BOT_USERNAME = "creditlogbot"

BUTTON_LABEL = os.getenv("BUTTON_LABEL", "🛍️ Open item")
BROADCAST_TO_USERS = os.getenv("BROADCAST_TO_USERS", "true").lower() in ("true", "1", "yes")

# Resolve DB_PATH safely (supports local dev and Docker /data/ paths)
db_env = os.getenv("DB_PATH", "store.db")
if db_env == ":memory:":
    DB_PATH = Path(":memory:")
elif db_env == "/data/store.db" and sys.platform == "win32" and not os.path.exists("/.dockerenv"):
    DB_PATH = ROOT_DIR / "store.db"
elif os.path.isabs(db_env):
    DB_PATH = Path(db_env)
elif (ROOT_DIR / db_env).exists():
    DB_PATH = ROOT_DIR / db_env
elif (ROOT_DIR / "store.db").exists():
    DB_PATH = ROOT_DIR / "store.db"
else:
    DB_PATH = ROOT_DIR / Path(db_env).name

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("StockBroadcaster")

def parse_source_chats(raw: str) -> List[str]:
    """Cleans and splits comma-separated source channels/bot usernames."""
    chats = []
    for item in raw.split(","):
        cleaned = item.strip()
        if not cleaned:
            continue
        if (cleaned.startswith("-") and cleaned[1:].isdigit()) or cleaned.isdigit():
            chats.append(int(cleaned))
        else:
            chats.append(cleaned)
    return chats

def is_broadcaster_enabled() -> bool:
    """Checks whether the automated stock broadcaster is active or paused."""
    env_val = os.getenv("BROADCASTER_ENABLED", "true").lower()
    if env_val in ("0", "false", "pause", "paused", "off"):
        return False

    if not DB_PATH.exists():
        return True

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT value FROM settings WHERE key='broadcaster_enabled'")
        row = cur.fetchone()
        if row and str(row[0]).strip().lower() in ("0", "false", "pause", "paused", "off"):
            return False
    except Exception as e:
        logger.debug(f"Could not read broadcaster_enabled from database: {e}")
    finally:
        if conn:
            conn.close()

    return True

def set_broadcaster_enabled(enabled: bool) -> None:
    """Persists broadcaster active/paused status in store.db settings."""
    if not DB_PATH.exists():
        return

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)")
        val = "1" if enabled else "0"
        cur.execute(
            "INSERT INTO settings(key, value) VALUES('broadcaster_enabled', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (val,)
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not save broadcaster_enabled setting: {e}")
    finally:
        if conn:
            conn.close()

def sync_product_from_broadcast(
    product_name: str,
    cost_usd: float = 0.0,
    new_price_usd: Optional[float] = None
) -> Optional[int]:
    """
    Synchronizes broadcast restock/price information with store.db:
    1. Looks up product by name using exact, substring, or brand-token matching.
    2. Updates supplier_price = cost_cents, active = 1, supplier_stock = 10.
    3. If fixed_price == 0: updates retail selling price to match store/product margin.
    4. If fixed_price == 1: preserves user-locked retail price and updates effective margin.
    5. If product is not found in store.db, auto-creates it under the appropriate category.
    Returns the matched or newly created product ID.
    """
    if not DB_PATH.exists():
        return None

    conn = None
    try:
        import re
        def clean_words(s: str) -> List[str]:
            cleaned = re.sub(r'[^a-zA-Z0-9\s]', ' ', s).lower()
            return [w for w in cleaned.split() if w]

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(products)")
        cols = {row[1] for row in cur.fetchall()}
        has_pricing = "price" in cols
        has_supplier = "supplier" in cols
        has_sp_id = "supplier_product_id" in cols

        if has_pricing:
            sup_col = "supplier" if has_supplier else "'renqis_retail'"
            spid_col = "supplier_product_id" if has_sp_id else "NULL"
            cur.execute(f"SELECT id, name, active, price, fixed_price, margin, supplier_price, {sup_col}, {spid_col} FROM products")
            rows = cur.fetchall()
        else:
            cur.execute("SELECT id, name, active FROM products")
            rows = [(r[0], r[1], r[2], 0, 0, 0, 0, "renqis_retail", None) for r in cur.fetchall()]

        q_words = clean_words(product_name)
        q_set = set(q_words)
        q_lower = ' '.join(q_words)
        cost_cents = int(round(cost_usd * 100)) if (cost_usd is not None and cost_usd > 0) else None

        GENERIC = {'api', 'account', 'redeem', 'credits', 'pack', 'plan', 'pro', 'month', 'months', 'year', 'years', '1m', '1y', '3m', '6m', '12m', '1', '2', '3', '6', '12', 'days', '7d', '28'}
        QUALIFIERS = {
            'solo', 'fam', 'hydro', 'hydrogen', 'plus', 'team', 'invite', 'cdk', 'business',
            'go', 'ultra', 'max', 'basic', 'lite', 'edu', 'mini', 'nano', 'pix', 'upi',
            'ggpay', 'gpay', 'apple', 'google', 'backup', 'your', 'mail', 'email',
            '1m', '3m', '4m', '6m', '12m', '24m', '1d', '2d', '3d', '4d', '6d', '7d', '14d', '28d', '30d', '1y', '2y', '3y',
        }
        q_quals = {w for w in q_words if w in QUALIFIERS or any(ch.isdigit() for ch in w)}

        matched_row = None

        # 1. Exact match pass: prefer matching price variant when multiple exist, active first then inactive
        for price_match_first in ((True, False) if cost_cents else (False,)):
            for active_target in (1, 0):
                for row in rows:
                    pid, name, active, price, fixed_price, margin, supplier_price, _, _ = row
                    if active != active_target:
                        continue
                    if price_match_first and abs((supplier_price or 0) - cost_cents) > 1:
                        continue
                    if ' '.join(clean_words(name)) == q_lower:
                        matched_row = row
                        break
                if matched_row:
                    break
            if matched_row:
                break

        # 2. Substring pass (requires qualifier parity so variants like Hydro Solo vs Hydrogen FAM never collide)
        if not matched_row:
            for active_target in (1, 0):
                for row in rows:
                    pid, name, active, price, fixed_price, margin, supplier_price, _, _ = row
                    if active != active_target:
                        continue
                    n_words = clean_words(name)
                    n_lower = ' '.join(n_words)
                    if not n_lower:
                        continue
                    n_quals = {w for w in n_words if w in QUALIFIERS or any(ch.isdigit() for ch in w)}
                    if n_quals != q_quals:
                        continue
                    brand_token = next((w for w in n_words if w not in GENERIC), n_words[0] if n_words else "")
                    if brand_token and brand_token in q_set and (n_lower in q_lower or q_lower in n_lower):
                        matched_row = row
                        break
                if matched_row:
                    break

        # 3. High-confidence token match with strict qualifier parity
        if not matched_row:
            best_match = None
            best_score = 0.0
            for active_target in (1, 0):
                for row in rows:
                    pid, name, active, price, fixed_price, margin, supplier_price, _, _ = row
                    if active != active_target:
                        continue
                    n_words = clean_words(name)
                    if not n_words:
                        continue
                    n_quals = {w for w in n_words if w in QUALIFIERS or any(ch.isdigit() for ch in w)}
                    if n_quals != q_quals:
                        continue
                    brand_token = next((w for w in n_words if w not in GENERIC), n_words[0])
                    if brand_token not in q_set:
                        continue
                    n_set = set(n_words)
                    score = len(q_set & n_set) / len(n_set | q_set)
                    if score > best_score and score >= 0.8:
                        best_score = score
                        best_match = row
                if best_match:
                    matched_row = best_match
                    break

        if matched_row:
            pid, name, active, price, fixed_price, margin, supplier_price, r_sup, r_spid = matched_row
            is_api_row = bool(r_sup == "default" and r_spid and not str(r_spid).startswith("renqis_retail_"))
            # Preserve exact Reseller API stock when >0 rather than overwriting it with 10
            if is_api_row:
                next_stock_clause = "active = 1, supplier_stock = CASE WHEN supplier_stock IS NULL OR supplier_stock <= 0 THEN 1 ELSE supplier_stock END"
            else:
                next_stock_clause = "active = 1, supplier_stock = 10"

            if has_pricing and cost_cents is not None and cost_cents > 0 and not is_api_row:
                if fixed_price == 1:
                    eff_margin = int(round(((price - cost_cents) / cost_cents) * 100)) if (price and price > 0) else (margin or 45)
                    cur.execute(
                        f"UPDATE products SET supplier_price = ?, margin = ?, {next_stock_clause} WHERE id = ?",
                        (cost_cents, eff_margin, pid)
                    )
                else:
                    if new_price_usd is not None and new_price_usd > 0:
                        retail_cents = int(round(new_price_usd * 100))
                    else:
                        m_val = margin if margin is not None else 45
                        retail_cents = int(round(cost_cents * (1 + m_val / 100.0)))
                    cur.execute(
                        f"UPDATE products SET price = ?, supplier_price = ?, {next_stock_clause} WHERE id = ?",
                        (retail_cents, cost_cents, pid)
                    )
            else:
                cur.execute(f"UPDATE products SET {next_stock_clause} WHERE id = ?", (pid,))

            conn.commit()
            logger.info(f"Broadcast sync updated SKU #{pid} ({name}): active=1, cost={cost_cents}c")
            return pid

        # 4. If product not found in store.db and has valid cost, auto-create it as renqis_retail
        if cost_cents is not None and cost_cents > 0:
            clean_name = re.sub(r'^[^\w\d]+', '', product_name).strip() or product_name.strip()
            cat = "AI Tools"
            try:
                from retail_catalog import detect_category
                cat = detect_category(clean_name)
            except Exception:
                pass

            if new_price_usd is not None and new_price_usd > 0:
                retail_cents = int(round(new_price_usd * 100))
            else:
                retail_cents = int(round(cost_cents * 1.45))

            desc = f"{clean_name}\n\nManual fulfillment by Store Admin within minutes."
            norm_slug = re.sub(r'[^a-z0-9]', '', clean_name.lower())
            sp_id = f"renqis_retail_{norm_slug}_{cost_cents}" if norm_slug else f"renqis_retail_{cost_cents}"
            if has_sp_id and has_supplier:
                cur.execute("""
                    INSERT INTO products (
                        name, description, price, active, supplier_product_id, supplier_price, margin, category, supplier, supplier_stock, delivery_type, fixed_price
                    ) VALUES (?, ?, ?, 1, ?, ?, 45, ?, 'renqis_retail', 10, 'manual', 0)
                """, (clean_name, desc, retail_cents, sp_id, cost_cents, cat))
            else:
                cur.execute("""
                    INSERT INTO products (
                        name, description, price, active, supplier_price, margin, category, supplier, supplier_stock, delivery_type, fixed_price
                    ) VALUES (?, ?, ?, 1, ?, 45, ?, 'renqis_retail', 10, 'manual', 0)
                """, (clean_name, desc, retail_cents, cost_cents, cat))
            conn.commit()
            new_id = cur.lastrowid
            logger.info(f"Broadcast sync created NEW SKU #{new_id} ({clean_name}) under category '{cat}' at {retail_cents}c")
            return new_id

    except Exception as e:
        logger.warning(f"Could not sync product in store.db: {e}")
    finally:
        if conn:
            conn.close()

    return None

def find_store_product_id(product_name: str) -> Optional[int]:
    """Finds matching product ID in store.db to generate direct ?start=p_<id> deep links."""
    return sync_product_from_broadcast(product_name)

def create_client() -> Optional[TelegramClient]:
    """Creates a Telethon client using StringSession or persistent session file."""
    if TelegramClient is None:
        logger.warning("Telethon is not installed. Broadcaster cannot start.")
        return None

    api_id_val = os.getenv("TELEGRAM_API_ID", API_ID or "")
    api_id_clean = str(api_id_val).strip().strip('"\'')
    api_hash_clean = str(os.getenv("TELEGRAM_API_HASH", API_HASH or "")).strip().strip('"\'')

    if not api_id_clean or not api_hash_clean:
        logger.info("Stock broadcaster disabled: TELEGRAM_API_ID or TELEGRAM_API_HASH not set.")
        return None

    try:
        api_id_int = int(api_id_clean)
    except ValueError:
        logger.error(f"Invalid TELEGRAM_API_ID: '{api_id_clean}' is not an integer.")
        return None

    string_session_val = os.getenv("TELEGRAM_STRING_SESSION", STRING_SESSION or "").strip().strip('"\'')
    if string_session_val:
        logger.info("Using Telethon StringSession from environment.")
        return TelegramClient(StringSession(string_session_val), api_id_int, api_hash_clean)

    # Use persistent session storage in DB_PATH directory (e.g. /data/ on Fly.io)
    session_dir = DB_PATH.parent if DB_PATH.parent.exists() else BASE_DIR
    session_target = str(session_dir / SESSION_NAME)
    return TelegramClient(session_target, api_id_int, api_hash_clean)

async def broadcast_to_bot_users(text: str, button_text: str, bot_url: str, aiogram_bot=None):
    """Broadcasts restock alert to all registered bot users in store.db."""
    if not DB_PATH.exists():
        return

    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        users = [row[0] for row in conn.execute("SELECT id FROM users").fetchall()]
    except Exception as e:
        logger.warning(f"Could not load users from store.db: {e}")
        return
    finally:
        if conn:
            conn.close()

    if not users:
        return

    logger.info(f"Broadcasting restock alert to {len(users)} registered bot users...")

    if aiogram_bot is not None:
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            try:
                btn = InlineKeyboardButton(text=button_text, url=bot_url, style="success")
            except TypeError:
                btn = InlineKeyboardButton(text=button_text, url=bot_url)
            markup = InlineKeyboardMarkup(inline_keyboard=[[btn]])
            sent = 0
            for uid in users:
                try:
                    await aiogram_bot.send_message(uid, text, reply_markup=markup, parse_mode=None)
                    sent += 1
                except Exception:
                    pass
                await asyncio.sleep(0.05)
            logger.info(f"Broadcast to bot users finished: {sent}/{len(users)} delivered.")
            return
        except Exception as e:
            logger.warning(f"aiogram broadcast encountered error: {e}")

    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if not bot_token:
        return

    try:
        import aiohttp
        api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload_markup = {
            "inline_keyboard": [[{"text": button_text, "url": bot_url, "style": "success"}]]
        }
        sent = 0
        async with aiohttp.ClientSession() as session:
            for uid in users:
                payload = {
                    "chat_id": uid,
                    "text": text,
                    "reply_markup": payload_markup
                }
                try:
                    async with session.post(api_url, json=payload, timeout=5) as resp:
                        if resp.status == 200:
                            sent += 1
                except Exception:
                    pass
                await asyncio.sleep(0.05)
        logger.info(f"Broadcast to bot users finished: {sent}/{len(users)} delivered.")
    except Exception as e:
        logger.error(f"Fallback bot users broadcast failed: {e}")

async def send_channel_broadcast(chat_id: str, text: str, button_text: str, bot_url: str, telethon_client, aiogram_bot=None):
    """Sends restock post to channel using Bot API with inline buttons (or user fallback)."""
    target = str(chat_id).strip()
    if (target.startswith("-") and target[1:].isdigit()) or target.isdigit():
        target_chat = int(target)
    else:
        target_chat = target

    # 1. Primary: Use aiogram_bot so Telegram renders native inline keyboard buttons
    if aiogram_bot is not None:
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            try:
                btn = InlineKeyboardButton(text=button_text, url=bot_url, style="success")
            except TypeError:
                btn = InlineKeyboardButton(text=button_text, url=bot_url)
            markup = InlineKeyboardMarkup(inline_keyboard=[[btn]])
            await aiogram_bot.send_message(target_chat, text, reply_markup=markup, parse_mode=None)
            logger.info(f"Successfully broadcasted to {target_chat} via Bot with green inline button!")
            return
        except Exception as e:
            logger.warning(f"Could not send to channel via aiogram_bot: {e}. Trying Bot API fallback...")

    # 2. Secondary: Bot API via aiohttp if running standalone
    bot_token = os.getenv("BOT_TOKEN", "").strip()
    if bot_token:
        try:
            import aiohttp
            api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                "chat_id": target_chat,
                "text": text,
                "reply_markup": {
                    "inline_keyboard": [[{"text": button_text, "url": bot_url, "style": "success"}]]
                }
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        logger.info(f"Successfully broadcasted to {target_chat} via Bot API with green inline button!")
                        return
                    else:
                        err_text = await resp.text()
                        logger.warning(f"Bot API sendMessage returned {resp.status}: {err_text}")
        except Exception as e:
            logger.warning(f"Bot API request failed: {e}")

    # 3. Fallback: Telethon user client
    try:
        await telethon_client.send_message(
            target_chat,
            text,
            buttons=[Button.url(button_text, bot_url)] if Button else None,
            link_preview=False
        )
        logger.info(f"Broadcasted to {target_chat} via Telethon user client.")
    except Exception as e:
        logger.error(f"Failed to post to {target_chat} via Telethon: {e}")

def init_broadcaster_db():
    """Ensure broadcaster_processed_messages table exists in store.db."""
    if not DB_PATH.exists() and str(DB_PATH) != ":memory:":
        return
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS broadcaster_processed_messages (
                msg_id INTEGER PRIMARY KEY,
                source_id TEXT,
                processed_at TEXT
            )
        """)
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not init broadcaster_processed_messages table: {e}")
    finally:
        if conn:
            conn.close()

def is_message_processed(msg_id: int) -> bool:
    """Checks whether a supplier message ID was already transformed and broadcasted."""
    if not DB_PATH.exists() and str(DB_PATH) != ":memory:":
        return False
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        row = conn.execute("SELECT 1 FROM broadcaster_processed_messages WHERE msg_id = ?", (msg_id,)).fetchone()
        return bool(row)
    except Exception:
        return False
    finally:
        if conn:
            conn.close()

def mark_message_processed(msg_id: int, source_id: str = ""):
    """Marks a message ID as processed in store.db to guarantee zero duplicates."""
    if not DB_PATH.exists() and str(DB_PATH) != ":memory:":
        return
    conn = None
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT OR IGNORE INTO broadcaster_processed_messages (msg_id, source_id, processed_at) VALUES (?, ?, ?)",
            (msg_id, str(source_id), datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
    except Exception as e:
        logger.warning(f"Could not mark message #{msg_id} processed: {e}")
    finally:
        if conn:
            conn.close()

async def process_and_broadcast_message(
    telethon_client: TelegramClient,
    parser: MessageParser,
    aiogram_bot,
    msg_id: int,
    raw_text: str,
    source_id: str = ""
) -> bool:
    """
    Transforms supplier restock text, syncs store.db catalog, and posts to channel & bot users.
    Returns True if an alert was parsed and broadcasted, False otherwise.
    """
    if not raw_text or not raw_text.strip():
        return False

    if is_message_processed(msg_id):
        logger.debug(f"Message #{msg_id} already processed. Skipping duplicate.")
        return False

    if not is_broadcaster_enabled():
        logger.info("Stock broadcaster is currently paused. Skipping incoming restock alert.")
        return False

    # Strictly reject sensitive/private chats, order receipts, and bot cards before parsing
    if MessageParser.is_sensitive_or_private(raw_text):
        logger.warning(f"Message #{msg_id} from {source_id} rejected: contains sensitive/private interaction content.")
        mark_message_processed(msg_id, source_id)
        return False

    result = parser.process_message(raw_text)
    if not result:
        # Mark non-restock menu/status messages so catch-up watchdog doesn't re-scan them forever
        mark_message_processed(msg_id, source_id)
        return False

    logger.info(f"Processing restock alert (msg #{msg_id} from {source_id})...")
    for item in result["items"]:
        logger.info(
            f"-> {item['name']}: Supplier Price = {item['old_price']} {item['currency']} "
            f"-> Your Price = {item['new_price']} {item['currency']}"
        )

    transformed_text = result["transformed_text"]
    button_text = result.get("button_text") or BUTTON_LABEL

    active_bot_username = MY_BOT_USERNAME
    if aiogram_bot is not None:
        try:
            me = await aiogram_bot.get_me()
            if me and me.username:
                active_bot_username = me.username.lstrip("@")
        except Exception:
            pass
    if active_bot_username.lower() in ("creditlog_bot", "credilog_bot", "creditlog"):
        active_bot_username = "creditlogbot"

    bot_url = f"https://t.me/{active_bot_username}"
    if result["type"] == "single" and result["items"]:
        item = result["items"][0]
        item_name = item["name"]
        matched_id = sync_product_from_broadcast(
            product_name=item_name,
            cost_usd=item.get("old_price", 0.0),
            new_price_usd=item.get("new_price")
        )
        if matched_id:
            bot_url = f"https://t.me/{active_bot_username}?start=p_{matched_id}"
            logger.info(f"Matched & synced product in store.db: #{matched_id} -> {bot_url}")
    elif result["type"] == "list":
        button_text = "🛍️ Open Store"
        for item in result.get("items", []):
            sync_product_from_broadcast(
                product_name=item["name"],
                cost_usd=item.get("old_price", 0.0),
                new_price_usd=item.get("new_price")
            )

    # 1. Broadcast to channel with inline button
    if DESTINATION_CHANNEL:
        await send_channel_broadcast(
            chat_id=DESTINATION_CHANNEL,
            text=transformed_text,
            button_text=button_text,
            bot_url=bot_url,
            telethon_client=telethon_client,
            aiogram_bot=aiogram_bot
        )
    else:
        logger.warning("DESTINATION_CHANNEL not set. Transformed preview:\n" + transformed_text)

    # 2. Also broadcast directly to registered store bot users in the background
    if BROADCAST_TO_USERS:
        asyncio.create_task(broadcast_to_bot_users(
            text=transformed_text,
            button_text=button_text,
            bot_url=bot_url,
            aiogram_bot=aiogram_bot
        ))

    mark_message_processed(msg_id, source_id)
    return True

async def catch_up_recent_broadcasts(client, parser, aiogram_bot, sources, limit: int = 15, my_user_id: Optional[int] = None):
    """
    Scans the last `limit` messages from each configured source chat.
    Processes any supplier restock alert that arrived while offline or reconnecting.
    Processes messages in chronological order (oldest first).
    """
    logger.info("Running catch-up scan on recent supplier messages...")
    for source in sources:
        try:
            entity = await client.get_entity(source)
            msgs = []
            now = datetime.now(timezone.utc)
            async for m in client.iter_messages(entity, limit=limit):
                if not m or not (m.message or m.text):
                    continue
                # Strictly skip outgoing messages sent by the user account
                if getattr(m, "out", False):
                    continue
                if my_user_id and getattr(m, "sender_id", None) == my_user_id:
                    continue
                # Skip messages older than 48 hours to avoid stale historical spam
                if m.date and (now - m.date).total_seconds() > 172800:
                    continue
                if not is_message_processed(m.id):
                    msgs.append(m)

            if msgs:
                logger.info(f"Found {len(msgs)} unprocessed recent message(s) from {source}.")
                # Reverse to process oldest first (chronological order)
                msgs.reverse()
                for m in msgs:
                    try:
                        raw = m.message or m.text or ""
                        success = await process_and_broadcast_message(
                            telethon_client=client,
                            parser=parser,
                            aiogram_bot=aiogram_bot,
                            msg_id=m.id,
                            raw_text=raw,
                            source_id=str(source)
                        )
                        if success:
                            await asyncio.sleep(1.5)
                    except Exception as ex:
                        logger.warning(f"Error during catch-up for msg #{m.id}: {ex}")
            else:
                logger.info(f"All recent messages from {source} are already up to date.")
        except Exception as e:
            logger.warning(f"Could not run catch-up scan for source {source}: {e}")

async def catch_up_watchdog(client, parser, aiogram_bot, sources, interval_secs: int = 300, my_user_id: Optional[int] = None):
    """Periodic watchdog that scans for any supplier messages missed during push glitches."""
    while True:
        try:
            await asyncio.sleep(interval_secs)
            if client and hasattr(client, "is_connected") and client.is_connected() and is_broadcaster_enabled():
                await catch_up_recent_broadcasts(client, parser, aiogram_bot, sources, limit=5, my_user_id=my_user_id)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.debug(f"Broadcaster watchdog error: {e}")

_active_client = None

def get_active_client():
    global _active_client
    if _active_client and hasattr(_active_client, "is_connected") and _active_client.is_connected():
        return _active_client
    return None

async def start_broadcaster_task(aiogram_bot=None):
    """
    Background task entry point for bot.py.
    Runs with auto-reconnect supervisor to ensure resilient 24/7 operation.
    """
    global _active_client
    init_broadcaster_db()
    reconnect_delay = 5

    while True:
        client = None
        watchdog_task = None
        try:
            client = create_client()
            if client is None:
                logger.info("Telethon client not configured. Stock broadcaster exiting.")
                return

            await client.connect()
            _active_client = client
            if not await client.is_user_authorized():
                logger.warning(
                    "Stock broadcaster: Telegram session is not yet authorized. "
                    "Run 'python broadcaster/login_session.py' locally to generate your TELEGRAM_STRING_SESSION "
                    "and set it in Fly.io secrets or .env."
                )
                await client.disconnect()
                return

            client_me = await client.get_me()
            my_user_id = client_me.id if client_me else None

            sources = parse_source_chats(SOURCE_CHATS_RAW)
            if sources:
                logger.info(f"Stock broadcaster listening to: {sources}")
            else:
                logger.info("Stock broadcaster listening to all incoming chats.")

            logger.info(f"Stock broadcaster publishing to: {DESTINATION_CHANNEL}")

            pricing_engine = PricingEngine(BASE_DIR / "pricing_rules.json", db_path=DB_PATH)
            parser = MessageParser(pricing_engine)

            filter_kwargs = {"chats": sources} if sources else {}

            @client.on(events.NewMessage(incoming=True, **filter_kwargs))
            async def handler(event):
                try:
                    # Strictly ignore outgoing messages or user-authored messages
                    if getattr(event, "out", False) or getattr(event.message, "out", False):
                        logger.debug("Skipping outgoing message sent by user.")
                        return

                    if my_user_id and event.sender_id == my_user_id:
                        logger.debug("Skipping message sent by own user account.")
                        return

                    raw_text = event.message.message or event.message.text or ""
                    msg_id = getattr(event.message, "id", None) or getattr(event, "id", None)
                    if not msg_id:
                        return
                    await process_and_broadcast_message(
                        telethon_client=client,
                        parser=parser,
                        aiogram_bot=aiogram_bot,
                        msg_id=msg_id,
                        raw_text=raw_text,
                        source_id=str(event.chat_id)
                    )
                except Exception as ex:
                    logger.exception(f"Unhandled error in stock broadcaster event handler: {ex}")

            logger.info("Stock broadcaster background service active.")
            reconnect_delay = 5

            # Catch up on any supplier alerts received while offline / in past hours
            if sources:
                await catch_up_recent_broadcasts(client, parser, aiogram_bot, sources, limit=15, my_user_id=my_user_id)
                watchdog_task = asyncio.create_task(
                    catch_up_watchdog(client, parser, aiogram_bot, sources, interval_secs=300, my_user_id=my_user_id)
                )

            await client.run_until_disconnected()
            logger.warning("Telethon client disconnected. Reconnecting in %ds...", reconnect_delay)

        except asyncio.CancelledError:
            logger.info("Stock broadcaster task cancelled. Shutting down client...")
            break
        except Exception as e:
            err_str = str(e).lower()
            if "two different ip addresses" in err_str or "authkeyduplicated" in err_str:
                logger.error(
                    "Telethon session key revoked by Telegram (used simultaneously on two different IPs). "
                    "Generate a fresh session with 'python broadcaster/login_session.py' and set TELEGRAM_STRING_SESSION. "
                    "Pausing broadcaster for 30 minutes..."
                )
                reconnect_delay = 1800
            else:
                logger.error(f"Stock broadcaster error: {e}. Reconnecting in {reconnect_delay}s...")
        finally:
            _active_client = None
            if watchdog_task and not watchdog_task.done():
                watchdog_task.cancel()
            try:
                if client and hasattr(client, "is_connected") and client.is_connected():
                    await client.disconnect()
            except Exception:
                pass

        await asyncio.sleep(reconnect_delay)
        reconnect_delay = min(reconnect_delay * 2, 60)

async def main():
    await start_broadcaster_task()

if __name__ == "__main__":
    asyncio.run(main())
