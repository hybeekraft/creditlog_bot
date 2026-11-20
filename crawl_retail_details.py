import asyncio
import os
import re
import json
import logging
import sqlite3
from telethon import TelegramClient, errors
from telethon.sessions import StringSession
from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("retail_crawler")

ENV_PATH = r"C:\Users\DELL\creditlogbot\broadcaster\.env"
load_dotenv(ENV_PATH)

API_ID = int(os.getenv("TELEGRAM_API_ID"))
API_HASH = os.getenv("TELEGRAM_API_HASH")
RAW_SESSION = os.getenv("TELEGRAM_STRING_SESSION", "").strip().strip('\'\"')

TARGET_BOT = "@geminiprosub_bot"
DB_PATH = r"C:\Users\DELL\creditlogbot\store.db"
DETAILS_JSON = r"C:\Users\DELL\creditlogbot\retail_catalog_details.json"

def norm_name(s: str) -> str:
    return re.sub(r'[^a-z0-9]', '', s.lower())

def parse_detail_text(text: str):
    clean = text.replace('\u2063', '').replace('\u2062', '').replace('\xa0', ' ').strip()
    
    item_match = re.search(r'(?:Item:\s*\*?\*?)(.*?)(?:\*?\*?\n)', clean)
    item_name = item_match.group(1).strip() if item_match else None
    
    desc = ""
    desc_match = re.search(r'Description:\s*\*?\*?(.*?)(?=(?:💵|\*\*Price|\n\s*Price:))', clean, re.DOTALL)
    if desc_match:
        desc = desc_match.group(1).strip()
        
    price = None
    price_match = re.search(r'Price[:\*]*\s*([\d\.]+)', clean)
    if price_match:
        price = float(price_match.group(1))
        
    stock = 0
    stock_match = re.search(r'In stock[:\*]*\s*(\d+)', clean)
    if stock_match:
        stock = int(stock_match.group(1))
        
    return {
        "item_name": item_name,
        "description": desc,
        "price_usd": price,
        "stock": stock
    }

def update_db_product(name: str, desc: str, stock: int, cost_usd: float = None):
    try:
        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        norm = norm_name(name)
        
        rows = cur.execute("SELECT id, name, margin, fixed_price FROM products WHERE supplier='renqis_retail'").fetchall()
        matched_id = None
        margin = 45
        fixed_price = 0
        for pid, pname, pmargin, pfixed in rows:
            if norm_name(pname) == norm:
                matched_id = pid
                margin = pmargin if pmargin is not None else 45
                fixed_price = pfixed
                break
                
        if matched_id:
            active = 1 if stock > 0 else 0
            if cost_usd is not None and not fixed_price:
                cost_cents = int(round(cost_usd * 100))
                retail_cents = max(100, int(round(cost_cents * (1 + margin / 100.0))))
                cur.execute("""
                    UPDATE products
                    SET description = ?,
                        supplier_stock = ?,
                        active = ?,
                        supplier_price = ?,
                        price = ?
                    WHERE id = ?
                """, (desc, stock, active, cost_cents, retail_cents, matched_id))
            else:
                cur.execute("""
                    UPDATE products
                    SET description = ?,
                        supplier_stock = ?,
                        active = ?
                    WHERE id = ?
                """, (desc, stock, active, matched_id))
            conn.commit()
            logger.info(f"Updated store.db: ID #{matched_id} ({name}) -> Stock: {stock}, Active: {active}")
        conn.close()
    except Exception as e:
        logger.error(f"Error updating DB for {name}: {e}")

async def open_catalog(client, bot):
    """Sends /start -> Shop -> All items to get the fresh Catalog message."""
    logger.info("Opening fresh catalog message from /start...")
    await client.send_message(bot, "/start")
    await asyncio.sleep(2.5)
    
    start_msg = (await client.get_messages(bot, limit=1))[0]
    cat_id = start_msg.id
    
    await start_msg.click(text="Shop")
    await asyncio.sleep(2.5)
    
    shop_msg = await client.get_messages(bot, ids=cat_id)
    await shop_msg.click(text="All items")
    await asyncio.sleep(2.5)
    
    cat_msg = await client.get_messages(bot, ids=cat_id)
    return cat_msg

async def crawl():
    # Load cache
    details_cache = {}
    if os.path.exists(DETAILS_JSON):
        try:
            with open(DETAILS_JSON, 'r', encoding='utf-8') as f:
                details_cache = json.load(f)
            logger.info(f"Loaded {len(details_cache)} items from cache {DETAILS_JSON}")
        except Exception as e:
            logger.warning(f"Could not load cache: {e}")
            
    client = TelegramClient(StringSession(RAW_SESSION), API_ID, API_HASH)
    await client.connect()
    bot = await client.get_entity(TARGET_BOT)
    
    cat_msg = await open_catalog(client, bot)
    cat_id = cat_msg.id
    logger.info(f"Catalog message ID: {cat_id}")
    
    total_pages = 19
    current_page = 1
    
    while current_page <= total_pages:
        cat_msg = await client.get_messages(bot, ids=cat_id)
        
        # Parse buttons
        page_indicator = f"{current_page}/{total_pages}"
        prod_buttons = []
        next_btn = None
        
        for r in getattr(cat_msg.reply_markup, 'rows', []):
            for b in r.buttons:
                data = getattr(getattr(b, 'type', None), 'data', None)
                if re.match(r'^\d+/\d+$', b.text.strip()):
                    page_indicator = b.text.strip()
                elif '▶️' in b.text:
                    next_btn = b
                elif data and data.startswith(b'itm:'):
                    prod_buttons.append((b.text, data))
                    
        logger.info(f"=== Scanning Page [{page_indicator}] | Products on page: {len(prod_buttons)} ===")
        
        # Scrape products on this page
        for idx, (b_text, b_data) in enumerate(prod_buttons, 1):
            clean_btn = b_text.replace('\u2063', '').replace('\u2062', '').replace('\xa0', ' ').strip()
            btn_name = clean_btn.split("·")[0].strip() if "·" in clean_btn else clean_btn
            norm_k = norm_name(btn_name)
            
            # Check if already cached with valid description
            if norm_k in details_cache and details_cache[norm_k].get("description") and len(details_cache[norm_k]["description"]) > 30:
                cached = details_cache[norm_k]
                logger.info(f"[{idx}/{len(prod_buttons)}] Page {current_page}: '{btn_name}' already cached. Syncing DB...")
                update_db_product(btn_name, cached["description"], cached.get("stock", 0), cached.get("price_usd"))
                continue
                
            logger.info(f"[{idx}/{len(prod_buttons)}] Page {current_page}: Clicking [{btn_name}]...")
            for attempt in range(3):
                try:
                    await cat_msg.click(text=b_text)
                    await asyncio.sleep(2.0)
                    detail_msg = await client.get_messages(bot, ids=cat_id)
                    parsed = parse_detail_text(detail_msg.text)
                    
                    if not parsed["item_name"]:
                        parsed["item_name"] = btn_name
                    if not parsed["description"]:
                        parsed["description"] = detail_msg.text
                        
                    parsed["page"] = page_indicator
                    parsed["raw_btn"] = b_text
                    
                    details_cache[norm_k] = parsed
                    with open(DETAILS_JSON, 'w', encoding='utf-8') as f:
                        json.dump(details_cache, f, indent=2, ensure_ascii=False)
                        
                    update_db_product(parsed["item_name"], parsed["description"], parsed["stock"], parsed.get("price_usd"))
                    logger.info(f"-> OK: '{parsed['item_name']}' | Stock: {parsed['stock']} pcs | Desc len: {len(parsed['description'])}")
                    
                    # Click Back
                    for r_back in getattr(detail_msg.reply_markup, 'rows', []):
                        for b_back in r_back.buttons:
                            if 'back' in b_back.text.lower():
                                await detail_msg.click(text=b_back.text)
                                break
                    await asyncio.sleep(2.0)
                    cat_msg = await client.get_messages(bot, ids=cat_id)
                    break
                except errors.FloodWaitError as fe:
                    logger.warning(f"Telegram FloodWait: sleeping {fe.seconds + 2}s...")
                    await asyncio.sleep(fe.seconds + 2)
                except Exception as ex:
                    logger.error(f"Error on [{btn_name}] (attempt {attempt+1}): {ex}")
                    await asyncio.sleep(2.5)
                    cat_msg = await client.get_messages(bot, ids=cat_id)
                    
        # Verify back on catalog before advancing
        cat_msg = await client.get_messages(bot, ids=cat_id)
        next_btn = None
        for r in getattr(cat_msg.reply_markup, 'rows', []):
            for b in r.buttons:
                if '▶️' in b.text:
                    next_btn = b
                    break
            if next_btn:
                break
                
        if current_page < total_pages and next_btn:
            logger.info(f"Navigating from Page {current_page} to Page {current_page + 1}...")
            try:
                await cat_msg.click(text=next_btn.text)
                await asyncio.sleep(2.5)
                current_page += 1
            except Exception as e:
                logger.error(f"Failed to navigate to page {current_page + 1}: {e}")
                break
        else:
            logger.info(f"Reached end of catalog (page {current_page}).")
            break
            
    await client.disconnect()
    logger.info(f"Crawl completed! Total items in cache: {len(details_cache)}")

if __name__ == "__main__":
    asyncio.run(crawl())
