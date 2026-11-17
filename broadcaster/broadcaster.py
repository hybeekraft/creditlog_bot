import os
import sys
import asyncio
import logging
import sqlite3
from pathlib import Path
from typing import List, Optional
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ROOT_DIR = BASE_DIR.parent

if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Load local broadcaster .env first, then root creditlog_bot .env
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
API_ID = os.getenv("TELEGRAM_API_ID")
API_HASH = os.getenv("TELEGRAM_API_HASH")
STRING_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "").strip()
SESSION_NAME = os.getenv("SESSION_NAME", "stock_broadcaster")

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

# Resolve DB_PATH safely (supports local dev and Docker /data/ paths)
db_env = os.getenv("DB_PATH", "store.db")
if (ROOT_DIR / "store.db").exists():
    DB_PATH = ROOT_DIR / "store.db"
elif Path(db_env).exists():
    DB_PATH = Path(db_env)
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

def find_store_product_id(product_name: str) -> Optional[int]:
    """Finds matching product ID in store.db to generate direct ?start=p_<id> deep links."""
    if not DB_PATH.exists():
        return None

    try:
        import re
        def clean_words(s: str) -> List[str]:
            cleaned = re.sub(r'[^a-zA-Z0-9\s]', ' ', s).lower()
            return [w for w in cleaned.split() if w]

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT id, name, active FROM products")
        rows = cur.fetchall()

        q_words = clean_words(product_name)
        q_set = set(q_words)
        q_lower = ' '.join(q_words)

        # 1. Direct substring in normalized text (active first, then inactive)
        for active_target in (1, 0):
            for pid, name, active in rows:
                if active != active_target:
                    continue
                n_words = clean_words(name)
                n_lower = ' '.join(n_words)
                if n_lower and (n_lower in q_lower or q_lower in n_lower):
                    if active == 0:
                        cur.execute("UPDATE products SET active = 1, supplier_stock = 10 WHERE id = ?", (pid,))
                        conn.commit()
                    conn.close()
                    return pid

        # 2. Key brand match: first non-generic word of db name MUST be in query
        GENERIC = {'api', 'account', 'redeem', 'credits', 'pack', 'plan', 'pro', 'month', 'months', 'year', 'years', '1m', '1y', '3m', '6m', '12m', '1', '2', '3', '6', '12', 'days', '7d', '28'}
        best_match = None
        best_score = 0.0

        for active_target in (1, 0):
            for pid, name, active in rows:
                if active != active_target:
                    continue
                n_words = clean_words(name)
                if not n_words:
                    continue
                brand_token = next((w for w in n_words if w not in GENERIC), n_words[0])
                if brand_token not in q_set:
                    continue
                n_set = set(n_words)
                common = q_set & n_set
                score = len(common) / len(n_set)
                if score > best_score and score >= 0.6:
                    best_score = score
                    best_match = (pid, active)
            if best_match:
                break

        if best_match:
            pid, active = best_match
            if active == 0:
                cur.execute("UPDATE products SET active = 1, supplier_stock = 10 WHERE id = ?", (pid,))
                conn.commit()
            conn.close()
            return pid

        conn.close()
    except Exception as e:
        logger.warning(f"Could not query store.db for product match: {e}")

    return None

def create_client() -> Optional[TelegramClient]:
    """Creates a Telethon client using StringSession or persistent session file."""
    if TelegramClient is None:
        logger.warning("Telethon is not installed. Broadcaster cannot start.")
        return None

    if not API_ID or not API_HASH:
        logger.info("Stock broadcaster disabled: TELEGRAM_API_ID or TELEGRAM_API_HASH not set.")
        return None

    if STRING_SESSION:
        logger.info("Using Telethon StringSession from environment.")
        return TelegramClient(StringSession(STRING_SESSION), int(API_ID), API_HASH)

    # Use persistent session storage in DB_PATH directory (e.g. /data/ on Fly.io)
    session_dir = DB_PATH.parent if DB_PATH.parent.exists() else BASE_DIR
    session_target = str(session_dir / SESSION_NAME)
    return TelegramClient(session_target, int(API_ID), API_HASH)

async def broadcast_to_bot_users(text: str, button_text: str, bot_url: str, aiogram_bot=None):
    """Broadcasts restock alert to all registered bot users in store.db."""
    if not DB_PATH.exists():
        return

    try:
        conn = sqlite3.connect(DB_PATH)
        users = [row[0] for row in conn.execute("SELECT id FROM users").fetchall()]
        conn.close()
    except Exception as e:
        logger.warning(f"Could not load users from store.db: {e}")
        return

    if not users:
        return

    logger.info(f"Broadcasting restock alert to {len(users)} registered bot users...")

    if aiogram_bot is not None:
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=button_text, url=bot_url)]
            ])
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
            "inline_keyboard": [[{"text": button_text, "url": bot_url}]]
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
    # 1. Primary: Use aiogram_bot so Telegram renders native inline keyboard buttons
    if aiogram_bot is not None:
        try:
            from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
            markup = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text=button_text, url=bot_url)]
            ])
            await aiogram_bot.send_message(chat_id, text, reply_markup=markup, parse_mode=None)
            logger.info(f"Successfully broadcasted to {chat_id} via Bot with inline button!")
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
                "chat_id": chat_id,
                "text": text,
                "reply_markup": {
                    "inline_keyboard": [[{"text": button_text, "url": bot_url}]]
                }
            }
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=payload, timeout=10) as resp:
                    if resp.status == 200:
                        logger.info(f"Successfully broadcasted to {chat_id} via Bot API with inline button!")
                        return
                    else:
                        err_text = await resp.text()
                        logger.warning(f"Bot API sendMessage returned {resp.status}: {err_text}")
        except Exception as e:
            logger.warning(f"Bot API request failed: {e}")

    # 3. Fallback: Telethon user client
    try:
        await telethon_client.send_message(
            chat_id,
            text,
            buttons=[Button.url(button_text, bot_url)] if Button else None,
            link_preview=False
        )
        logger.info(f"Broadcasted to {chat_id} via Telethon user client.")
    except Exception as e:
        logger.error(f"Failed to post to {chat_id} via Telethon: {e}")

async def start_broadcaster_task(aiogram_bot=None):
    """Background task entry point for bot.py."""
    client = create_client()
    if client is None:
        return

    try:
        await client.connect()
        if not await client.is_user_authorized():
            logger.warning(
                "Stock broadcaster: Telegram session is not yet authorized. "
                "Run 'python broadcaster/login_session.py' locally to generate your TELEGRAM_STRING_SESSION "
                "and set it in Fly.io secrets or .env."
            )
            await client.disconnect()
            return

        sources = parse_source_chats(SOURCE_CHATS_RAW)
        if sources:
            logger.info(f"Stock broadcaster listening to: {sources}")
        else:
            logger.info("Stock broadcaster listening to all incoming chats.")

        logger.info(f"Stock broadcaster publishing to: {DESTINATION_CHANNEL}")

        pricing_engine = PricingEngine(BASE_DIR / "pricing_rules.json")
        parser = MessageParser(pricing_engine)

        filter_kwargs = {"chats": sources} if sources else {}

        @client.on(events.NewMessage(**filter_kwargs))
        async def handler(event):
            raw_text = event.message.message or ""
            if not raw_text:
                return

            result = parser.process_message(raw_text)
            if not result:
                return

            logger.info("Detected new restock alert from supplier! Processing markup...")
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
                item_name = result["items"][0]["name"]
                matched_id = find_store_product_id(item_name)
                if matched_id:
                    bot_url = f"https://t.me/{active_bot_username}?start=p_{matched_id}"
                    logger.info(f"Matched product in store.db: #{matched_id} -> {bot_url}")
            elif result["type"] == "list":
                button_text = "🛍️ Open Store"

            buttons = [
                [Button.url(button_text, bot_url)]
            ]

            # 1. Broadcast to channel with inline button
            if DESTINATION_CHANNEL:
                await send_channel_broadcast(
                    chat_id=DESTINATION_CHANNEL,
                    text=transformed_text,
                    button_text=button_text,
                    bot_url=bot_url,
                    telethon_client=client,
                    aiogram_bot=aiogram_bot
                )
            else:
                logger.warning("DESTINATION_CHANNEL not set. Transformed preview:\n" + transformed_text)

            # Also broadcast directly to registered store bot users
            try:
                await broadcast_to_bot_users(
                    text=transformed_text,
                    button_text=button_text,
                    bot_url=bot_url,
                    aiogram_bot=aiogram_bot
                )
            except Exception as e:
                logger.error(f"Failed to broadcast to bot users: {e}")

        logger.info("Stock broadcaster background service active.")
        await client.run_until_disconnected()
    except asyncio.CancelledError:
        logger.info("Stock broadcaster task cancelled. Shutting down client...")
    except Exception as e:
        logger.error(f"Stock broadcaster encountered an unexpected error: {e}")
    finally:
        if client.is_connected():
            await client.disconnect()

async def main():
    await start_broadcaster_task()

if __name__ == "__main__":
    asyncio.run(main())
