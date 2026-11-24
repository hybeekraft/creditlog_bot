import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
from typing import Callable, Optional

from dotenv import load_dotenv

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("retail_crawler")

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / "broadcaster" / ".env"
ROOT_ENV = BASE_DIR / ".env"

load_dotenv(ENV_PATH)
load_dotenv(ROOT_ENV)

TARGET_BOT = os.getenv("SUPPLIER_RETAIL_BOT", "@geminiprosub_bot")
DETAILS_JSON = BASE_DIR / "retail_catalog_details.json"


def resolve_db_path(custom_path=None) -> Path:
    if custom_path:
        return Path(custom_path)
    db_env = os.getenv("DB_PATH", "store.db")
    if db_env == ":memory:":
        return Path(":memory:")
    if os.path.isabs(db_env):
        return Path(db_env)
    return BASE_DIR / db_env


def norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_catalog_button(b_text: str) -> dict:
    """Extracts clean product name, supplier price (USD), and live in-stock status from catalog button text."""
    clean = b_text.replace("\u2063", "").replace("\u2062", "").replace("\xa0", " ").strip()
    btn_name = clean.split("·")[0].strip() if "·" in clean else clean

    # Strip trailing emojis or marks from name
    btn_name = re.sub(r"[\s\u200b\ufe0f]*[✅🟢✔️⛔🔴🚫❌➖—]+\s*$", "", btn_name).strip()

    price_usd = None
    price_match = re.search(r"·\s*\$?\s*([\d\.]+)", clean)
    if not price_match:
        price_match = re.search(r"\$\s*([\d\.]+)", clean)
    if price_match:
        try:
            price_usd = float(price_match.group(1))
        except ValueError:
            price_usd = None

    is_in_stock = True
    if any(em in clean for em in ("⛔", "🔴", "🚫", "❌", "➖", "—")):
        is_in_stock = False
    elif any(em in clean for em in ("✅", "🟢", "✔️")):
        is_in_stock = True

    stock = 10 if is_in_stock else 0

    return {
        "name": btn_name,
        "price_usd": price_usd,
        "is_in_stock": is_in_stock,
        "stock": stock,
    }


def parse_detail_text(text: str) -> dict:
    clean = text.replace("\u2063", "").replace("\u2062", "").replace("\xa0", " ").strip()

    item_match = re.search(r"(?:Item:\s*\*?\*?)(.*?)(?:\*?\*?\n)", clean)
    item_name = item_match.group(1).strip() if item_match else None

    desc = ""
    desc_match = re.search(r"Description:\s*\*?\*?(.*?)(?=(?:💵|\*\*Price|\n\s*Price:))", clean, re.DOTALL)
    if desc_match:
        desc = desc_match.group(1).strip()

    price = None
    price_match = re.search(r"Price[:\*]*\s*([\d\.]+)", clean)
    if price_match:
        price = float(price_match.group(1))

    stock = 0
    stock_match = re.search(r"In stock[:\*]*\s*(\d+)", clean)
    if stock_match:
        stock = int(stock_match.group(1))

    return {
        "item_name": item_name,
        "description": desc,
        "price_usd": price,
        "stock": stock,
    }


def update_db_product(name: str, desc: str, stock: int, cost_usd: Optional[float] = None, db_path=None):
    """Updates an existing product (default or renqis_retail) or inserts a new retail product if not found."""
    conn = None
    try:
        target_path = resolve_db_path(db_path)
        conn = sqlite3.connect(target_path)
        cur = conn.cursor()
        norm = norm_name(name)

        rows = cur.execute("SELECT id, name, margin, fixed_price, supplier, auto_fulfill, description FROM products").fetchall()
        matched_id = None
        matched_desc = ""
        margin = 45
        fixed_price = 0

        # Prioritize matching an existing product. If multiple exist, prioritize auto_fulfill/default
        candidates = []
        for pid, pname, pmargin, pfixed, psup, pauto, pdesc in rows:
            if norm_name(pname) == norm:
                candidates.append((pid, pname, pmargin, pfixed, psup, pauto, pdesc))

        if candidates:
            # Prefer supplier='default' or auto_fulfill=1
            candidates.sort(key=lambda c: (1 if (c[4] == 'default' or c[5]) else 0, -c[0]), reverse=True)
            matched_id, _, pmargin, fixed_price, _, _, matched_desc = candidates[0]
            margin = pmargin if pmargin is not None else 45

        if matched_id:
            active = 1 if stock > 0 else 0
            effective_desc = desc if (desc and (not matched_desc or len(matched_desc) < len(desc))) else matched_desc
            if cost_usd is not None and not fixed_price:
                cost_cents = int(round(cost_usd * 100))
                retail_cents = max(100, int(round(cost_cents * (1 + margin / 100.0))))
                cur.execute(
                    """
                    UPDATE products
                    SET description = ?,
                        supplier_stock = ?,
                        active = ?,
                        supplier_price = ?,
                        price = ?
                    WHERE id = ?
                """,
                    (effective_desc, stock, active, cost_cents, retail_cents, matched_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE products
                    SET description = ?,
                        supplier_stock = ?,
                        active = ?
                    WHERE id = ?
                """,
                    (effective_desc, stock, active, matched_id),
                )
            conn.commit()
            return matched_id, False
        else:
            cat = "AI Tools"
            try:
                from bot import detect_category

                cat = detect_category(name)
            except Exception:
                pass
            cost_cents = int(round(cost_usd * 100)) if (cost_usd is not None and cost_usd > 0) else 0
            retail_cents = max(100, int(round(cost_cents * 1.45)))
            active = 1 if stock > 0 else 0
            sp_id = f"renqis_retail_{norm}"
            cur.execute(
                """
                INSERT INTO products (
                    name, description, price, supplier_product_id, supplier_price,
                    auto_fulfill, active, category, supplier, supplier_stock,
                    delivery_type, margin, fixed_price
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'renqis_retail', ?, 'manual', 45, 0)
            """,
                (name, desc, retail_cents, sp_id, cost_cents, active, cat, stock),
            )
            conn.commit()
            new_id = cur.lastrowid
            logger.info("Inserted NEW SKU into store.db: ID #%d (%s) -> Stock: %d, Active: %d", new_id, name, stock, active)
            return new_id, True
    except Exception as e:
        logger.error("Error updating DB for %s: %s", name, e)
        return None, False
    finally:
        if conn:
            conn.close()


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


async def run_retail_crawler(
    db_path=None,
    progress_cb: Optional[Callable[[int, int, str], asyncio.Future]] = None,
    force_refresh_all: bool = False,
) -> dict:
    """Connects to supplier retail bot via Telethon, scrapes catalog items & descriptions,

    and syncs them directly into store.db and retail_catalog_details.json.
    """
    raw_api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    raw_session = os.getenv("TELEGRAM_STRING_SESSION", "").strip().strip("'\"")

    if not (raw_api_id and api_hash and raw_session):
        msg = "Telethon credentials not configured in broadcaster/.env"
        logger.warning(msg)
        return {
            "success": False,
            "error": msg,
            "total_scraped": 0,
            "new_items": 0,
            "updated_items": 0,
            "pages_scanned": 0,
        }

    try:
        api_id = int(raw_api_id)
    except ValueError:
        return {"success": False, "error": "Invalid TELEGRAM_API_ID format", "total_scraped": 0, "new_items": 0, "updated_items": 0, "pages_scanned": 0}

    # Lazy import to avoid dependency issues when telethon isn't needed
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession

    # Load cache
    details_cache = {}
    if os.path.exists(DETAILS_JSON):
        try:
            with open(DETAILS_JSON, "r", encoding="utf-8") as f:
                details_cache = json.load(f)
            logger.info("Loaded %d items from cache %s", len(details_cache), DETAILS_JSON)
        except Exception as e:
            logger.warning("Could not load cache: %s", e)

    client = TelegramClient(StringSession(raw_session), api_id, api_hash)
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        return {"success": False, "error": "Telegram session is unauthorized or expired", "total_scraped": 0, "new_items": 0, "updated_items": 0, "pages_scanned": 0}

    new_count = 0
    updated_count = 0
    current_page = 1
    total_pages = 19

    try:
        bot = await client.get_entity(TARGET_BOT)
        cat_msg = await open_catalog(client, bot)
        cat_id = cat_msg.id
        logger.info("Catalog message ID: %d", cat_id)

        while current_page <= total_pages:
            cat_msg = await client.get_messages(bot, ids=cat_id)

            # Parse buttons
            page_indicator = f"{current_page}/{total_pages}"
            prod_buttons = []
            next_btn = None

            for r in getattr(cat_msg.reply_markup, "rows", []):
                for b in r.buttons:
                    data = getattr(getattr(b, "type", None), "data", None)
                    if re.match(r"^\d+/\d+$", b.text.strip()):
                        page_indicator = b.text.strip()
                        try:
                            total_pages = max(total_pages, int(page_indicator.split("/")[1]))
                        except (ValueError, IndexError):
                            pass
                    elif "▶️" in b.text:
                        next_btn = b
                    elif data and data.startswith(b"itm:"):
                        prod_buttons.append((b.text, data))

            logger.info("=== Scanning Page [%s] | Products: %d ===", page_indicator, len(prod_buttons))
            if progress_cb:
                try:
                    await progress_cb(current_page, total_pages, f"Scanning page {page_indicator} ({len(prod_buttons)} items)...")
                except Exception:
                    pass

            for idx, (b_text, b_data) in enumerate(prod_buttons, 1):
                btn_info = parse_catalog_button(b_text)
                btn_name = btn_info["name"]
                live_price = btn_info["price_usd"]
                live_stock = btn_info["stock"]
                norm_k = norm_name(btn_name)

                # Skip deep click if already cached with a rich description, unless forced
                if not force_refresh_all and norm_k in details_cache and details_cache[norm_k].get("description") and len(details_cache[norm_k]["description"]) > 30:
                    cached = details_cache[norm_k]
                    eff_price = live_price if live_price is not None else cached.get("price_usd")
                    eff_stock = live_stock
                    cached["stock"] = eff_stock
                    if live_price is not None:
                        cached["price_usd"] = live_price
                    pid, is_new = update_db_product(btn_name, cached["description"], eff_stock, eff_price, db_path)
                    if is_new:
                        new_count += 1
                    else:
                        updated_count += 1
                    continue

                # Click item button to scrape full description and live piece count
                logger.info("[%d/%d] Page %d: Scraping new details for [%s]...", idx, len(prod_buttons), current_page, btn_name)
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
                        if parsed.get("price_usd") is None and live_price is not None:
                            parsed["price_usd"] = live_price
                        if parsed.get("stock", 0) == 0 and live_stock > 0:
                            parsed["stock"] = live_stock

                        parsed["page"] = page_indicator
                        parsed["raw_btn"] = b_text

                        details_cache[norm_k] = parsed
                        with open(DETAILS_JSON, "w", encoding="utf-8") as f:
                            json.dump(details_cache, f, indent=2, ensure_ascii=False)

                        pid, is_new = update_db_product(parsed["item_name"], parsed["description"], parsed["stock"], parsed.get("price_usd"), db_path)
                        if is_new:
                            new_count += 1
                        else:
                            updated_count += 1

                        logger.info("-> OK: '%s' | Stock: %d pcs", parsed["item_name"], parsed["stock"])

                        # Return to catalog by clicking Back button
                        for r_back in getattr(detail_msg.reply_markup, "rows", []):
                            for b_back in r_back.buttons:
                                if "back" in b_back.text.lower():
                                    await detail_msg.click(text=b_back.text)
                                    break
                        await asyncio.sleep(2.0)
                        cat_msg = await client.get_messages(bot, ids=cat_id)
                        break
                    except errors.FloodWaitError as fe:
                        logger.warning("Telegram FloodWait: sleeping %ds...", fe.seconds + 2)
                        await asyncio.sleep(fe.seconds + 2)
                    except Exception as ex:
                        logger.error("Error scraping [%s] (attempt %d): %s", btn_name, attempt + 1, ex)
                        await asyncio.sleep(2.5)
                        cat_msg = await client.get_messages(bot, ids=cat_id)

            # Advance to next page
            cat_msg = await client.get_messages(bot, ids=cat_id)
            next_btn = None
            for r in getattr(cat_msg.reply_markup, "rows", []):
                for b in r.buttons:
                    if "▶️" in b.text:
                        next_btn = b
                        break
                if next_btn:
                    break

            if current_page < total_pages and next_btn:
                try:
                    await cat_msg.click(text=next_btn.text)
                    await asyncio.sleep(2.5)
                    current_page += 1
                except Exception as e:
                    logger.error("Failed to advance to page %d: %s", current_page + 1, e)
                    break
            else:
                break

        return {
            "success": True,
            "total_scraped": len(details_cache),
            "new_items": new_count,
            "updated_items": updated_count,
            "pages_scanned": current_page,
            "error": None,
        }
    except Exception as e:
        logger.error("Retail crawler failed: %s", e)
        return {
            "success": False,
            "error": str(e),
            "total_scraped": len(details_cache),
            "new_items": new_count,
            "updated_items": updated_count,
            "pages_scanned": current_page,
        }
    finally:
        await client.disconnect()


if __name__ == "__main__":
    asyncio.run(run_retail_crawler())
