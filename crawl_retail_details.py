import asyncio
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
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

MENU_SCREEN_MARKERS = (
    "welcome to gemini pro store",
    "choose a category",
    "select a section or open the full catalog",
    "choose a product",
)


def is_menu_screen_text(text: Optional[str]) -> bool:
    """Returns True if message text is a bot navigation/menu screen rather than product details."""
    if not text:
        return True
    low = text.lower()
    if "in stock:" in low or ("item:" in low and "price:" in low):
        return False
    return any(marker in low for marker in MENU_SCREEN_MARKERS)


def resolve_db_path(custom_path=None) -> Path:
    if custom_path:
        return Path(custom_path)
    db_env = os.getenv("DB_PATH", "store.db")
    if db_env == ":memory:":
        return Path(":memory:")
    if db_env == "/data/store.db" and sys.platform == "win32" and not os.path.exists("/.dockerenv"):
        return BASE_DIR / "store.db"
    if os.path.isabs(db_env):
        return Path(db_env)
    return BASE_DIR / db_env


def resolve_details_json_path(db_path=None) -> Path:
    """Returns persistent retail_catalog_details.json path alongside /data DB when writable."""
    default_json = BASE_DIR / "retail_catalog_details.json"
    if DETAILS_JSON != default_json:
        return DETAILS_JSON
    target_db = resolve_db_path(db_path)
    if str(target_db) != ":memory:":
        db_dir = target_db.parent
        db_env = os.getenv("DB_PATH", "")
        if db_dir != BASE_DIR and db_dir.exists() and os.access(db_dir, os.W_OK) and (
            str(db_dir) == "/data" or db_env.startswith("/data/")
        ):
            return db_dir / "retail_catalog_details.json"
    return DETAILS_JSON


def update_retail_catalogs(details_cache: dict) -> bool:
    """Updates both retail_catalog.py and retail_bot_catalog_raw.json directly from scraped details."""
    cat_py_path = BASE_DIR / "retail_catalog.py"
    raw_json_path = BASE_DIR / "retail_bot_catalog_raw.json"
    if not details_cache:
        return False
    try:
        items = []
        raw_items = []
        for k, v in details_cache.items():
            name = v.get("item_name") or k
            price = round(float(v.get("price_usd") or 0.0), 2)
            stock = int(v.get("stock") or 0)
            in_stock = stock > 0
            items.append((name, price, in_stock))
            raw_btn = v.get("raw_btn") or f"{name} · ${price:.2f} {'✅' if in_stock else '⛔'}"
            raw_items.append({
                "name": name,
                "cost_usd": price,
                "is_in_stock": in_stock,
                "raw_button": raw_btn,
                "callback": "",
                "page": v.get("page", "")
            })

        items.sort(key=lambda x: x[0].lower())
        raw_items.sort(key=lambda x: x["name"].lower())

        if cat_py_path.exists():
            content = cat_py_path.read_text(encoding="utf-8")
            item_lines = [f"    ({repr(name)}, {price:.2f}, {in_stock})," for name, price, in_stock in items]
            replacement_block = "RETAIL_CATALOG_ITEMS = [\n" + "\n".join(item_lines) + "\n]"
            new_content, count = re.subn(
                r"RETAIL_CATALOG_ITEMS\s*=\s*\[.*?^\s*\]",
                lambda _: replacement_block,
                content,
                count=1,
                flags=re.DOTALL | re.MULTILINE,
            )
            if count > 0:
                cat_py_path.write_text(new_content, encoding="utf-8")
                logger.info("Successfully updated retail_catalog.py with %d items", len(items))

        with open(raw_json_path, "w", encoding="utf-8") as f:
            json.dump(raw_items, f, indent=2, ensure_ascii=False)
        logger.info("Successfully updated retail_bot_catalog_raw.json with %d items", len(raw_items))
        return True
    except Exception as e:
        logger.warning("Failed to update retail catalog files: %s", e)
        return False


update_retail_catalog_py = update_retail_catalogs


def norm_name(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def parse_catalog_button(b_text: str) -> dict:
    """Extracts clean product name, supplier price (USD), and live in-stock status from catalog button text."""
    clean = b_text.replace("\u2063", "").replace("\u2062", "").replace("\xa0", " ").strip()
    btn_name = clean.split("·")[0].strip() if "·" in clean else clean

    # Strip trailing emojis or marks from name
    btn_name = re.sub(r"[\s\u200b\ufe0f]*[✅🟢✔️⛔🔴🚫❌➖—]+\s*$", "", btn_name).strip()

    price_usd = None
    price_match = re.search(r"·\s*\$?\s*(\d+(?:\.\d+)?)", clean)
    if not price_match:
        # Match trailing price only so '$100' inside product titles is never parsed as unit cost
        price_match = re.search(r"\$\s*(\d+(?:\.\d+)?)\s*(?:[✅🟢✔️⛔🔴🚫❌➖—]*)\s*$", clean)
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
    clean = (text or "").replace("\u2063", "").replace("\u2062", "").replace("\xa0", " ").strip()

    item_match = re.search(r"Item:\s*\*?\*?([^\n\*]+)", clean)
    item_name = item_match.group(1).strip() if item_match else None

    desc = ""
    desc_match = re.search(r"Description:\s*\*?\*?(.*?)(?=(?:💵|\*\*Price|\n\s*Price:))", clean, re.DOTALL)
    if desc_match:
        desc = desc_match.group(1).strip()

    price = None
    price_match = re.search(r"Price[:\*\s]*\$?\s*(\d+(?:\.\d+)?)", clean)
    if price_match:
        price = float(price_match.group(1))

    stock = 0
    stock_match = re.search(r"In stock[:\*\s]*(\d+)", clean)
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
            clean_desc = "" if is_menu_screen_text(desc) else desc
            effective_desc = clean_desc if (clean_desc and (not matched_desc or len(matched_desc) < len(clean_desc))) else (matched_desc or "")
            cat = "AI Tools"
            try:
                from bot import detect_category
                cat = detect_category(name)
            except Exception:
                pass
            if cost_usd is not None and cost_usd > 0 and not fixed_price:
                cost_cents = int(round(cost_usd * 100))
                retail_cents = max(100, int(round(cost_cents * (1 + margin / 100.0))))
                cur.execute(
                    """
                    UPDATE products
                    SET description = ?,
                        supplier_stock = ?,
                        active = ?,
                        supplier_price = ?,
                        price = ?,
                        category = CASE WHEN category = 'AI Tools' OR category IS NULL THEN ? ELSE category END
                    WHERE id = ?
                """,
                    (effective_desc, stock, active, cost_cents, retail_cents, cat, matched_id),
                )
            else:
                cur.execute(
                    """
                    UPDATE products
                    SET description = ?,
                        supplier_stock = ?,
                        active = ?,
                        category = CASE WHEN category = 'AI Tools' OR category IS NULL THEN ? ELSE category END
                    WHERE id = ?
                """,
                    (effective_desc, stock, active, cat, matched_id),
                )
            conn.commit()
            return matched_id, False
        else:
            if cost_usd is None or cost_usd <= 0:
                logger.warning("Skipping new retail SKU [%s] with missing/zero price (%s)", name, cost_usd)
                return None, False
            cat = "AI Tools"
            try:
                from bot import detect_category

                cat = detect_category(name)
            except Exception:
                pass
            cost_cents = int(round(cost_usd * 100))
            retail_cents = max(100, int(round(cost_cents * 1.45)))
            active = 1 if stock > 0 else 0
            clean_desc = f"{name}\n\nVerified digital product delivery." if is_menu_screen_text(desc) else desc
            sp_id = f"renqis_retail_{norm}"
            cur.execute(
                """
                INSERT INTO products (
                    name, description, price, supplier_product_id, supplier_price,
                    auto_fulfill, active, category, supplier, supplier_stock,
                    delivery_type, margin, fixed_price
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'renqis_retail', ?, 'manual', 45, 0)
            """,
                (name, clean_desc, retail_cents, sp_id, cost_cents, active, cat, stock),
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


def is_product_button(clean_btn: str, data: Optional[bytes]) -> bool:
    """Returns True only for actual product buttons, rejecting menu/balance buttons."""
    if data and data.startswith(b"itm:"):
        return True
    low = clean_btn.lower()
    if any(skip in low for skip in ("balance", "deposit", "top up", "top-up", "wallet", "profile", "contact us")):
        return False
    return bool(
        "·" in clean_btn
        and (
            "$" in clean_btn
            or re.search(r"·\s*\$?\s*\d", clean_btn)
            or any(em in clean_btn for em in ("✅", "⛔", "🟢", "🔴", "🚫", "❌"))
        )
    )


def _find_button(msg, label: str):
    """Finds an inline button matching label (exact case-insensitive first, then substring)."""
    target = label.strip().lower()
    rows = getattr(getattr(msg, "reply_markup", None), "rows", []) or []
    partial = None
    for r in rows:
        for b in r.buttons:
            txt = (getattr(b, "text", "") or "").strip().lower()
            if txt == target:
                return b
            if partial is None and target in txt:
                partial = b
    return partial


def _find_button_any(msg, labels):
    """Finds the first inline button matching any candidate label in priority order."""
    for lbl in labels:
        btn = _find_button(msg, lbl)
        if btn is not None:
            return btn
    return None


def _button_signature(msg) -> tuple:
    rows = getattr(getattr(msg, "reply_markup", None), "rows", []) or []
    return tuple((getattr(b, "text", "") or "").strip() for r in rows for b in r.buttons)


async def _click_inline_button(msg, button):
    data = getattr(button, "data", None)
    if data is None and hasattr(button, "type"):
        data = getattr(button.type, "data", None)
    if data:
        return await msg.click(data=data)
    return await msg.click(text=button.text)


def _has_catalog_pagination(msg) -> bool:
    for r in getattr(getattr(msg, "reply_markup", None), "rows", []) or []:
        for b in r.buttons:
            if re.match(r"^\d+/\d+$", (getattr(b, "text", "") or "").strip()):
                return True
    return False


def _is_broadcast_alert_msg(msg) -> bool:
    """Returns True if message looks like an unsolicited supplier restock broadcast alert."""
    raw = getattr(msg, "raw_text", None) or getattr(msg, "message", None) or getattr(msg, "text", None)
    txt = raw.lower() if isinstance(raw, str) else ""
    if any(marker in txt for marker in ("restocked", "limited stock", "instant delivery after payment", "price:")):
        if "choose a product" not in txt and "choose a category" not in txt and not _has_catalog_pagination(msg):
            return True
    return False


async def _wait_for_incoming_reply(client, bot, after_id: int, timeout: float = 10.0):
    """Polls until the bot replies with an interactive menu newer than after_id (ignoring restock alerts)."""
    steps = max(1, int(timeout / 0.5))
    for _ in range(steps):
        recent = await client.get_messages(bot, limit=5)
        for msg in recent or []:
            if (
                not getattr(msg, "out", False)
                and msg.id > after_id
                and getattr(msg, "reply_markup", None)
                and not _is_broadcast_alert_msg(msg)
            ):
                return msg
        await asyncio.sleep(0.5)
    return None


async def _wait_for_menu_transition(
    client,
    bot,
    cur_msg,
    before_sig: tuple,
    timeout: float = 10.0,
    in_place_only: bool = False,
):
    """Waits for either an in-place edit on cur_msg or (when allowed) a newer bot menu message."""
    steps = max(1, int(timeout / 0.5))
    cur_id = cur_msg.id
    for _ in range(steps):
        await asyncio.sleep(0.5)
        edited = await client.get_messages(bot, ids=cur_id)
        if edited and getattr(edited, "reply_markup", None) and _button_signature(edited) != before_sig:
            return edited
        if not in_place_only:
            recent = await client.get_messages(bot, limit=3)
            for latest in recent or []:
                if (
                    not getattr(latest, "out", False)
                    and latest.id > cur_id
                    and getattr(latest, "reply_markup", None)
                    and not _is_broadcast_alert_msg(latest)
                ):
                    return latest
    # Fallback to latest known markup state if signature stayed identical
    if not in_place_only:
        recent = await client.get_messages(bot, limit=3)
        for latest in recent or []:
            if (
                not getattr(latest, "out", False)
                and latest.id > cur_id
                and getattr(latest, "reply_markup", None)
                and not _is_broadcast_alert_msg(latest)
            ):
                return latest
    edited = await client.get_messages(bot, ids=cur_id)
    return edited if (edited and getattr(edited, "reply_markup", None)) else cur_msg


async def _recover_to_catalog_page(client, bot, cat_id: int, msg):
    """Returns from an accidental detail screen back to the catalog list if needed."""
    if msg and _has_catalog_pagination(msg):
        return msg
    back_btn = _find_button(msg, "back") if msg else None
    if back_btn:
        try:
            before_sig = _button_signature(msg)
            await _click_inline_button(msg, back_btn)
            return await _wait_for_menu_transition(client, bot, msg, before_sig, timeout=6.0, in_place_only=True)
        except Exception:
            pass
    return await client.get_messages(bot, ids=cat_id)


async def open_catalog(client, bot):
    """Sends /start -> Shop -> All items to open the paginated Catalog message."""
    logger.info("Opening fresh catalog message from /start...")
    sent_msg = await client.send_message(bot, "/start")
    sent_id = getattr(sent_msg, "id", 0) or 0

    start_msg = await _wait_for_incoming_reply(client, bot, after_id=sent_id, timeout=10.0)
    if not start_msg:
        recent = await client.get_messages(bot, limit=1)
        start_msg = recent[0] if recent else None
    if not start_msg or not getattr(start_msg, "reply_markup", None):
        raise RuntimeError("No interactive menu received from supplier bot after /start")

    if _has_catalog_pagination(start_msg):
        return start_msg

    cur_msg = start_msg
    shop_btn = _find_button_any(cur_msg, ("shop", "catalog", "store", "products"))
    if shop_btn:
        before_sig = _button_signature(cur_msg)
        await _click_inline_button(cur_msg, shop_btn)
        cur_msg = await _wait_for_menu_transition(client, bot, cur_msg, before_sig, timeout=10.0)

    if _has_catalog_pagination(cur_msg):
        return cur_msg

    all_btn = _find_button_any(cur_msg, ("all items", "all products", "full catalog", "open catalog", "view all"))
    if all_btn:
        before_sig = _button_signature(cur_msg)
        await _click_inline_button(cur_msg, all_btn)
        cur_msg = await _wait_for_menu_transition(client, bot, cur_msg, before_sig, timeout=10.0)

    return cur_msg


def _persist_cache(details_cache: dict, primary_path: Path):
    with open(primary_path, "w", encoding="utf-8") as f:
        json.dump(details_cache, f, indent=2, ensure_ascii=False)
    if primary_path != DETAILS_JSON:
        try:
            with open(DETAILS_JSON, "w", encoding="utf-8") as f:
                json.dump(details_cache, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


def _deactivate_removed_retail_skus(seen_norms: set, db_path=None) -> int:
    """Marks renqis_retail SKUs missing from a complete supplier bot crawl as inactive."""
    if not seen_norms or len(seen_norms) < 50:
        return 0
    conn = None
    deactivated = 0
    try:
        conn = sqlite3.connect(resolve_db_path(db_path))
        cur = conn.cursor()
        rows = cur.execute(
            "SELECT id, name FROM products WHERE supplier='renqis_retail' AND active=1"
        ).fetchall()
        for pid, pname in rows:
            if norm_name(pname) not in seen_norms:
                cur.execute("UPDATE products SET active=0, supplier_stock=0 WHERE id=?", (pid,))
                deactivated += 1
        if deactivated:
            conn.commit()
            logger.info("Deactivated %d removed retail SKUs not present on supplier bot", deactivated)
    except Exception as e:
        logger.warning("Could not deactivate removed retail SKUs: %s", e)
    finally:
        if conn:
            conn.close()
    return deactivated


async def run_retail_crawler(
    db_path=None,
    progress_cb: Optional[Callable[[int, int, str], asyncio.Future]] = None,
    force_refresh_all: bool = False,
) -> dict:
    """Connects to supplier retail bot via Telethon, scrapes catalog items & descriptions,
    and syncs them directly into store.db and retail_catalog_details.json.
    """
    raw_api_id = (os.getenv("TELEGRAM_API_ID") or "").strip().strip("'\"")
    api_hash = (os.getenv("TELEGRAM_API_HASH") or "").strip().strip("'\"")
    raw_session = (os.getenv("TELEGRAM_STRING_SESSION") or "").strip().strip("'\"")

    # Lazy import to avoid dependency issues when telethon isn't needed
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession

    cache_path = resolve_details_json_path(db_path)
    candidate_paths = [DETAILS_JSON]
    if cache_path != DETAILS_JSON:
        candidate_paths.append(cache_path)

    details_cache = {}
    for candidate_path in candidate_paths:
        if os.path.exists(candidate_path):
            try:
                with open(candidate_path, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                for k, v in loaded.items():
                    if k in details_cache and is_menu_screen_text(v.get("description")) and not is_menu_screen_text(details_cache[k].get("description")):
                        continue
                    details_cache[k] = v
                logger.info("Loaded %d items from cache %s", len(loaded), candidate_path)
            except Exception as e:
                logger.warning("Could not load cache %s: %s", candidate_path, e)

    client = None
    own_client = False
    new_count = 0
    updated_count = 0
    current_page = 1
    total_pages = 19
    seen_norms = set()

    try:
        try:
            from broadcaster.broadcaster import get_active_client
            client = get_active_client()
        except Exception:
            client = None

        if client is None or not client.is_connected():
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
                return {
                    "success": False,
                    "error": "Invalid TELEGRAM_API_ID format",
                    "total_scraped": 0,
                    "new_items": 0,
                    "updated_items": 0,
                    "pages_scanned": 0,
                }

            client = TelegramClient(StringSession(raw_session), api_id, api_hash)
            own_client = True
            await client.connect()
            if not await client.is_user_authorized():
                return {
                    "success": False,
                    "error": "Telegram session is unauthorized or expired",
                    "total_scraped": 0,
                    "new_items": 0,
                    "updated_items": 0,
                    "pages_scanned": 0,
                }

        bot = await client.get_entity(TARGET_BOT)
        cat_msg = await open_catalog(client, bot)
        if not cat_msg:
            raise RuntimeError("Could not open supplier catalog message")
        cat_id = cat_msg.id
        logger.info("Catalog message ID: %d", cat_id)

        while current_page <= total_pages:
            cat_msg = await client.get_messages(bot, ids=cat_id)
            if not cat_msg or not getattr(cat_msg, "reply_markup", None):
                logger.warning("Catalog message missing reply_markup on page %d; stopping.", current_page)
                break

            saw_pagination = False
            page_indicator = f"{current_page}/{total_pages}"
            prod_buttons = []
            all_btn_labels = []
            next_btn = None

            for r in getattr(cat_msg.reply_markup, "rows", []):
                for b in r.buttons:
                    data = getattr(b, "data", None)
                    if data is None and hasattr(b, "type"):
                        data = getattr(b.type, "data", None)
                    clean_btn = b.text.strip()
                    all_btn_labels.append(clean_btn)
                    if re.match(r"^\d+/\d+$", clean_btn):
                        saw_pagination = True
                        page_indicator = clean_btn
                        try:
                            total_pages = int(page_indicator.split("/")[1])
                        except (ValueError, IndexError):
                            pass
                    elif "▶️" in clean_btn or "➡️" in clean_btn:
                        next_btn = b
                    elif is_product_button(clean_btn, data):
                        prod_buttons.append((b.text, data))

            if current_page == 1 and (not prod_buttons or (not saw_pagination and len(prod_buttons) < 2)):
                raise RuntimeError(
                    f"Did not reach supplier catalog listing (visible buttons: {', '.join(all_btn_labels[:8])})"
                )

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
                seen_norms.add(norm_k)

                cached = details_cache.get(norm_k)
                cached_desc = (cached or {}).get("description", "")
                has_valid_cache = bool(
                    not force_refresh_all
                    and cached
                    and cached_desc
                    and len(cached_desc) > 30
                    and not is_menu_screen_text(cached_desc)
                    and not cached.get("needs_refresh")
                    and ((live_price is not None and live_price > 0) or (cached.get("price_usd") and float(cached.get("price_usd")) > 0))
                )
                if has_valid_cache:
                    eff_price = live_price if (live_price is not None and live_price > 0) else cached.get("price_usd")
                    eff_stock = live_stock
                    cached["stock"] = eff_stock
                    if eff_price is not None and eff_price > 0:
                        cached["price_usd"] = eff_price
                    cached["raw_btn"] = b_text
                    cached["page"] = page_indicator
                    pid, is_new = update_db_product(btn_name, cached_desc, eff_stock, eff_price, db_path)
                    if is_new:
                        new_count += 1
                    elif pid:
                        updated_count += 1
                    continue

                # Click item button to scrape full description and live piece count
                logger.info("[%d/%d] Page %d: Scraping new details for [%s]...", idx, len(prod_buttons), current_page, btn_name)
                scraped_ok = False
                for attempt in range(3):
                    try:
                        if attempt > 0:
                            cat_msg = await _recover_to_catalog_page(client, bot, cat_id, cat_msg)
                        if b_data:
                            await cat_msg.click(data=b_data)
                        else:
                            await cat_msg.click(text=b_text)
                        await asyncio.sleep(2.0)
                        detail_msg = await client.get_messages(bot, ids=cat_id)
                        detail_text = getattr(detail_msg, "text", "") or ""
                        if not detail_msg or is_menu_screen_text(detail_text):
                            logger.warning("Got non-product screen for [%s] on attempt %d", btn_name, attempt + 1)
                            cat_msg = await _recover_to_catalog_page(client, bot, cat_id, detail_msg or cat_msg)
                            await asyncio.sleep(1.5)
                            continue

                        parsed = parse_detail_text(detail_text)
                        if not parsed["item_name"]:
                            parsed["item_name"] = btn_name
                        if not parsed["description"]:
                            parsed["description"] = cached_desc if (cached_desc and not is_menu_screen_text(cached_desc)) else detail_text
                        if live_price is not None and live_price > 0:
                            # Prefer catalog button price when valid so '$100' inside titles doesn't override
                            if parsed.get("price_usd") is None or abs(float(parsed["price_usd"]) - live_price) > 0.01:
                                parsed["price_usd"] = live_price
                        if parsed.get("stock", 0) == 0 and live_stock > 0:
                            parsed["stock"] = live_stock

                        parsed["page"] = page_indicator
                        parsed["raw_btn"] = b_text
                        parsed.pop("needs_refresh", None)

                        details_cache[norm_k] = parsed
                        _persist_cache(details_cache, cache_path)

                        pid, is_new = update_db_product(parsed["item_name"], parsed["description"], parsed["stock"], parsed.get("price_usd"), db_path)
                        if is_new:
                            new_count += 1
                        elif pid:
                            updated_count += 1

                        logger.info("-> OK: '%s' | Stock: %d pcs", parsed["item_name"], parsed["stock"])

                        back_btn = _find_button(detail_msg, "back")
                        if back_btn:
                            await _click_inline_button(detail_msg, back_btn)
                            await asyncio.sleep(2.0)
                        cat_msg = await _recover_to_catalog_page(client, bot, cat_id, await client.get_messages(bot, ids=cat_id))
                        if cat_msg:
                            cat_id = cat_msg.id
                        scraped_ok = True
                        break
                    except errors.FloodWaitError as fe:
                        logger.warning("Telegram FloodWait: sleeping %ds...", fe.seconds + 2)
                        await asyncio.sleep(fe.seconds + 2)
                    except Exception as ex:
                        logger.error("Error scraping [%s] (attempt %d): %s", btn_name, attempt + 1, ex)
                        await asyncio.sleep(2.5)
                        cat_msg = await _recover_to_catalog_page(client, bot, cat_id, await client.get_messages(bot, ids=cat_id))
                        if cat_msg:
                            cat_id = cat_msg.id

                if not scraped_ok and live_price is not None and live_price > 0:
                    fallback_desc = cached_desc if (cached_desc and not is_menu_screen_text(cached_desc)) else f"{btn_name}\n\nVerified digital product delivery."
                    pid, is_new = update_db_product(btn_name, fallback_desc, live_stock, live_price, db_path)
                    if is_new:
                        new_count += 1
                    elif pid:
                        updated_count += 1

            # Advance to next page
            cat_msg = await client.get_messages(bot, ids=cat_id)
            next_btn = _find_button_any(cat_msg, ("▶️", "➡️")) if cat_msg else None

            if current_page < total_pages and next_btn:
                try:
                    before_sig = _button_signature(cat_msg)
                    await _click_inline_button(cat_msg, next_btn)
                    # Pagination edits the catalog message in place; ignore concurrent broadcast alerts
                    cat_msg = await _wait_for_menu_transition(
                        client, bot, cat_msg, before_sig, timeout=6.0, in_place_only=True
                    )
                    cat_id = cat_msg.id
                    current_page += 1
                except Exception as e:
                    logger.error("Failed to advance to page %d: %s", current_page + 1, e)
                    break
            else:
                break

        if current_page >= total_pages:
            _deactivate_removed_retail_skus(seen_norms, db_path=db_path)

        try:
            _persist_cache(details_cache, cache_path)
            update_retail_catalog_py(details_cache)
        except Exception as e:
            logger.warning("Could not persist scraped details: %s", e)

        return {
            "success": True,
            "total_scraped": len(details_cache),
            "new_items": new_count,
            "updated_items": updated_count,
            "pages_scanned": current_page,
            "error": None,
        }
    except Exception as e:
        err_str = str(e).lower()
        if "two different ip addresses" in err_str or "authkeyduplicated" in err_str:
            err_msg = (
                "Telethon session key revoked by Telegram (used simultaneously on two different IPs). "
                "Generate a fresh session with 'python broadcaster/login_session.py', set it on Fly.io only, "
                "and avoid reusing the same TELEGRAM_STRING_SESSION locally."
            )
            logger.error("Retail crawler failed: %s", err_msg)
        else:
            err_msg = str(e)
            logger.error("Retail crawler failed: %s", err_msg)
        return {
            "success": False,
            "error": err_msg,
            "total_scraped": len(details_cache),
            "new_items": new_count,
            "updated_items": updated_count,
            "pages_scanned": current_page,
        }
    finally:
        if own_client and client:
            try:
                await client.disconnect()
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(run_retail_crawler())
