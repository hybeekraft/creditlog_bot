import asyncio
import inspect
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any, Callable, Optional

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


def update_retail_catalogs(details_cache: dict, min_items: int = 50) -> bool:
    """Updates both retail_catalog.py and retail_bot_catalog_raw.json directly from scraped details."""
    cat_py_path = BASE_DIR / "retail_catalog.py"
    raw_json_path = BASE_DIR / "retail_bot_catalog_raw.json"
    if not details_cache or len(details_cache) < min_items or DETAILS_JSON != (BASE_DIR / "retail_catalog_details.json"):
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


def update_db_product(
    name: str,
    desc: str,
    stock: int,
    cost_usd: Optional[float] = None,
    db_path=None,
    allow_multi_price: bool = False,
):
    """Updates an existing product (preserving live API stock/prices on default rows) or inserts a new retail SKU."""
    conn = None
    try:
        target_path = resolve_db_path(db_path)
        conn = sqlite3.connect(target_path)
        cur = conn.cursor()
        norm = norm_name(name)
        target_cost_cents = int(round(cost_usd * 100)) if (cost_usd is not None and cost_usd > 0) else None

        rows = cur.execute(
            "SELECT id, name, margin, fixed_price, supplier, auto_fulfill, description, supplier_price FROM products"
        ).fetchall()
        has_delisted = any(r[1] == "delisted" for r in cur.execute("PRAGMA table_info(products)").fetchall())
        matched_id = None
        matched_desc = ""
        matched_is_api = False
        margin = 45
        fixed_price = 0

        candidates = []
        for pid, pname, pmargin, pfixed, psup, pauto, pdesc, psup_price in rows:
            if norm_name(pname) == norm:
                candidates.append((pid, pname, pmargin, pfixed, psup, pauto, pdesc, psup_price))

        if candidates:
            def _cand_key(c):
                c_cost = c[7] or 0
                price_hit = 1 if (target_cost_cents is not None and c_cost > 0 and abs(c_cost - target_cost_cents) <= 1) else 0
                is_api = 1 if (c[4] == "default" or c[5]) else 0
                return (price_hit, is_api, -c[0])

            candidates.sort(key=_cand_key, reverse=True)
            best = candidates[0]
            best_cost = best[7] or 0
            # Keep distinct-price variants separate when multiple prices exist on supplier bot
            price_mismatch = bool(
                allow_multi_price and target_cost_cents is not None and best_cost > 0 and abs(best_cost - target_cost_cents) > 1
            )
            if not price_mismatch:
                matched_id, _, pmargin, fixed_price, psup, pauto, matched_desc, _ = best
                matched_is_api = bool(psup == "default" or pauto)
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
            if matched_is_api:
                # Never overwrite live Reseller API stock, active status, or pricing with retail scrape guesses
                cur.execute(
                    """
                    UPDATE products
                    SET description = ?,
                        category = CASE WHEN category = 'AI Tools' OR category IS NULL THEN ? ELSE category END
                    WHERE id = ?
                """,
                    (effective_desc, cat, matched_id),
                )
            elif cost_usd is not None and cost_usd > 0 and not fixed_price:
                cost_cents = int(round(cost_usd * 100))
                retail_cents = max(100, int(round(cost_cents * (1 + margin / 100.0))))
                delisted_clause = ", delisted = 0" if has_delisted else ""
                cur.execute(
                    f"""
                    UPDATE products
                    SET description = ?,
                        supplier_stock = ?,
                        active = ?,
                        supplier_price = ?,
                        price = ?,
                        category = CASE WHEN category = 'AI Tools' OR category IS NULL THEN ? ELSE category END{delisted_clause}
                    WHERE id = ?
                """,
                    (effective_desc, stock, active, cost_cents, retail_cents, cat, matched_id),
                )
            else:
                delisted_clause = ", delisted = 0" if has_delisted else ""
                cur.execute(
                    f"""
                    UPDATE products
                    SET description = ?,
                        supplier_stock = ?,
                        active = ?,
                        category = CASE WHEN category = 'AI Tools' OR category IS NULL THEN ? ELSE category END{delisted_clause}
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
            sp_id = f"renqis_retail_{norm}_{cost_cents}" if allow_multi_price else f"renqis_retail_{norm}"
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


def _extract_page_num(msg) -> Optional[int]:
    for r in getattr(getattr(msg, "reply_markup", None), "rows", []) or []:
        for b in r.buttons:
            m = re.match(r"^(\d+)/\d+$", (getattr(b, "text", "") or "").strip())
            if m:
                return int(m.group(1))
    return None


def _has_catalog_pagination(msg) -> bool:
    return _extract_page_num(msg) is not None


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
    """Returns from an accidental detail screen back to the paginated catalog list."""
    if msg and _has_catalog_pagination(msg):
        return msg
    cur = msg or await client.get_messages(bot, ids=cat_id)
    if cur and _has_catalog_pagination(cur):
        return cur
    back_btn = _find_button(cur, "back") if cur else None
    if back_btn:
        try:
            before_sig = _button_signature(cur)
            await _click_inline_button(cur, back_btn)
            res = await _wait_for_menu_transition(client, bot, cur, before_sig, timeout=8.0, in_place_only=True)
            if res and _has_catalog_pagination(res):
                return res
        except Exception:
            pass
    # Fallback to newest incoming non-broadcast paginated message if Back opened a new message
    recent = await client.get_messages(bot, limit=5)
    for latest in recent or []:
        if (
            not getattr(latest, "out", False)
            and getattr(latest, "reply_markup", None)
            and not _is_broadcast_alert_msg(latest)
            and _has_catalog_pagination(latest)
        ):
            return latest
    return await client.get_messages(bot, ids=cat_id)


async def open_catalog(client, bot):
    """Sends /start -> Shop -> All items to open the paginated Catalog message."""
    logger.info("Opening fresh catalog message from /start...")
    sent_msg = await client.send_message(bot, "/start")
    sent_id = getattr(sent_msg, "id", 0) or 0

    start_msg = await _wait_for_incoming_reply(client, bot, after_id=sent_id, timeout=10.0)
    if not start_msg:
        recent = await client.get_messages(bot, limit=5)
        for m in recent or []:
            if not getattr(m, "out", False) and getattr(m, "reply_markup", None) and not _is_broadcast_alert_msg(m):
                start_msg = m
                break
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


_telethon_nav_lock = asyncio.Lock()


async def check_live_retail_item_stock(
    item_name: str,
    expected_price_usd: Optional[float] = None,
    page_hint: Optional[str] = None,
) -> Optional[dict]:
    """Quickly checks live stock for one retail item on @geminiprosub_bot via active Telethon client."""
    try:
        from broadcaster.broadcaster import get_active_client
        client = get_active_client()
    except Exception:
        return None
    if client is None or not client.is_connected():
        return None

    try:
        await asyncio.wait_for(_telethon_nav_lock.acquire(), timeout=3.0)
    except Exception:
        return None

    try:
        bot = await client.get_entity(TARGET_BOT)
        sent = await client.send_message(bot, "/start")
        cur_msg: Any = await _wait_for_incoming_reply(client, bot, after_id=getattr(sent, "id", 0) or 0, timeout=8.0)
        if not cur_msg:
            return None

        if not _has_catalog_pagination(cur_msg):
            shop_btn = _find_button_any(cur_msg, ("shop", "catalog", "store", "products"))
            if shop_btn:
                before_sig = _button_signature(cur_msg)
                await _click_inline_button(cur_msg, shop_btn)
                cur_msg = await _wait_for_menu_transition(client, bot, cur_msg, before_sig, timeout=8.0)

        # Jump directly into matching category screen when available (1-2 pages instead of 20)
        if not _has_catalog_pagination(cur_msg):
            cat_guess = ""
            try:
                from bot import detect_category
                cat_guess = detect_category(item_name).lower()
            except Exception:
                pass
            target_btn = _find_button(cur_msg, cat_guess) if cat_guess and cat_guess != "ai tools" else None
            if not target_btn:
                target_btn = _find_button_any(cur_msg, ("all items", "all products", "full catalog", "open catalog", "view all"))
            if target_btn:
                before_sig = _button_signature(cur_msg)
                await _click_inline_button(cur_msg, target_btn)
                cur_msg = await _wait_for_menu_transition(client, bot, cur_msg, before_sig, timeout=8.0)

        if not cur_msg or not getattr(cur_msg, "reply_markup", None):
            return None

        target_norm = norm_name(item_name)
        target_cents = int(round(expected_price_usd * 100)) if (expected_price_usd and expected_price_usd > 0) else None
        max_scan = 20 if page_hint else 5

        for _ in range(max_scan):
            for r in getattr(cur_msg.reply_markup, "rows", []):
                for b in r.buttons:
                    data = getattr(b, "data", None)
                    if data is None and hasattr(b, "type"):
                        data = getattr(b.type, "data", None)
                    clean_btn = (getattr(b, "text", "") or "").strip()
                    if not is_product_button(clean_btn, data):
                        continue
                    info = parse_catalog_button(clean_btn)
                    if norm_name(info["name"]) != target_norm:
                        continue
                    b_cents = int(round(info["price_usd"] * 100)) if info.get("price_usd") else None
                    if target_cents and b_cents and abs(b_cents - target_cents) > 1:
                        continue
                    if not info["is_in_stock"]:
                        return {"in_stock": False, "stock": 0, "price_usd": info.get("price_usd")}
                    # Click product button to confirm exact piece count
                    before_detail_sig = _button_signature(cur_msg)
                    await _click_inline_button(cur_msg, b)
                    detail_msg = await _wait_for_menu_transition(client, bot, cur_msg, before_detail_sig, timeout=6.0, in_place_only=True)
                    detail_txt = getattr(detail_msg, "text", "") or ""
                    if detail_msg and not is_menu_screen_text(detail_txt):
                        parsed = parse_detail_text(detail_txt)
                        live_qty = parsed.get("stock", 0)
                        return {
                            "in_stock": live_qty > 0,
                            "stock": live_qty,
                            "price_usd": info.get("price_usd") or parsed.get("price_usd"),
                        }
                    return {"in_stock": True, "stock": info.get("stock", 1), "price_usd": info.get("price_usd")}

            next_btn = _find_button_any(cur_msg, ("▶️", "➡️", "▶", "➡"))
            if not next_btn:
                break
            before_sig = _button_signature(cur_msg)
            await _click_inline_button(cur_msg, next_btn)
            next_msg = await _wait_for_menu_transition(client, bot, cur_msg, before_sig, timeout=6.0, in_place_only=True)
            if not next_msg or _button_signature(next_msg) == before_sig:
                break
            cur_msg = next_msg
    except Exception as e:
        logger.debug("Live retail stock check skipped for [%s]: %s", item_name, e)
    finally:
        if _telethon_nav_lock.locked():
            _telethon_nav_lock.release()
    return None


def _persist_cache(details_cache: dict, primary_path: Path):
    with open(primary_path, "w", encoding="utf-8") as f:
        json.dump(details_cache, f, indent=2, ensure_ascii=False)
    if primary_path != DETAILS_JSON:
        try:
            with open(DETAILS_JSON, "w", encoding="utf-8") as f:
                json.dump(details_cache, f, indent=2, ensure_ascii=False)
        except Exception:
            pass


def _ensure_delisted_column(cur) -> None:
    cols = {r[1] for r in cur.execute("PRAGMA table_info(products)").fetchall()}
    if "delisted" not in cols:
        cur.execute("ALTER TABLE products ADD COLUMN delisted INTEGER DEFAULT 0")


def remove_or_delist_product(conn_or_cur, pid: int) -> str:
    """Deletes unreferenced discontinued SKU, or marks delisted=1/active=0 when order/stock history exists."""
    cur = conn_or_cur.cursor() if hasattr(conn_or_cur, "cursor") else conn_or_cur
    _ensure_delisted_column(cur)
    has_orders = False
    has_unsold_stock = False
    try:
        has_orders = bool(cur.execute("SELECT 1 FROM orders WHERE product_id=? LIMIT 1", (pid,)).fetchone())
    except sqlite3.OperationalError:
        pass
    try:
        has_unsold_stock = bool(cur.execute("SELECT 1 FROM stock WHERE product_id=? AND sold=0 LIMIT 1", (pid,)).fetchone())
    except sqlite3.OperationalError:
        pass

    if has_orders or has_unsold_stock:
        cur.execute("UPDATE products SET active=0, supplier_stock=0, delisted=1 WHERE id=?", (pid,))
        return "delisted"

    try:
        cur.execute("DELETE FROM stock WHERE product_id=?", (pid,))
    except sqlite3.OperationalError:
        pass
    try:
        row_feat = cur.execute("SELECT value FROM settings WHERE key='featured_products'").fetchone()
        if row_feat and row_feat[0]:
            f_ids = [x.strip() for x in str(row_feat[0]).split(",") if x.strip() and x.strip() != str(pid)]
            cur.execute("UPDATE settings SET value=? WHERE key='featured_products'", (",".join(f_ids),))
    except sqlite3.OperationalError:
        pass
    cur.execute("DELETE FROM products WHERE id=?", (pid,))
    return "deleted"


def prune_stale_retail_catalog(
    details_cache: dict,
    seen_cache_keys: set,
    seen_norms: set,
    seen_db_ids: set,
    db_path=None,
    conn=None,
    min_seen: int = 50,
) -> dict:
    """Prunes stale keys from details_cache and deletes/delists removed renqis_retail rows."""
    if not seen_cache_keys or len(seen_cache_keys) < min_seen:
        return {"cache_pruned": 0, "deleted": 0, "delisted": 0}

    stale_keys = [k for k in list(details_cache.keys()) if k not in seen_cache_keys]
    for k in stale_keys:
        details_cache.pop(k, None)

    own_conn = conn is None
    deleted = 0
    delisted = 0
    try:
        if own_conn:
            conn = sqlite3.connect(resolve_db_path(db_path))
        cur = conn.cursor()
        _ensure_delisted_column(cur)
        rows = cur.execute("SELECT id, name FROM products WHERE supplier='renqis_retail'").fetchall()
        for r in rows:
            pid = r[0] if isinstance(r, tuple) else r["id"]
            if pid not in seen_db_ids:
                outcome = remove_or_delist_product(cur, pid)
                if outcome == "deleted":
                    deleted += 1
                else:
                    delisted += 1
        conn.commit()
        if stale_keys or deleted or delisted:
            logger.info(
                "Pruned retail catalog: %d stale cache keys removed, %d DB SKUs deleted, %d DB SKUs delisted",
                len(stale_keys), deleted, delisted,
            )
    except Exception as e:
        logger.warning("Could not prune stale retail catalog entries: %s", e)
    finally:
        if own_conn and conn:
            conn.close()
    return {"cache_pruned": len(stale_keys), "deleted": deleted, "delisted": delisted}


def _deactivate_removed_retail_skus(seen_norms: set, db_path=None) -> int:
    """Backwards-compatible helper marking renqis_retail SKUs missing from supplier bot as delisted/deleted."""
    if not seen_norms or len(seen_norms) < 50:
        return 0
    conn = None
    removed = 0
    try:
        conn = sqlite3.connect(resolve_db_path(db_path))
        cur = conn.cursor()
        _ensure_delisted_column(cur)
        rows = cur.execute("SELECT id, name FROM products WHERE supplier='renqis_retail'").fetchall()
        for pid, pname in rows:
            if norm_name(pname) not in seen_norms:
                remove_or_delist_product(cur, pid)
                removed += 1
        if removed:
            conn.commit()
    except Exception as e:
        logger.warning("Could not deactivate removed retail SKUs: %s", e)
    finally:
        if conn:
            conn.close()
    return removed


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
    # Use persistent /data cache exclusively once populated so pruned keys are never resurrected from /app
    if cache_path != DETAILS_JSON and cache_path.exists():
        candidate_paths = [cache_path]
    else:
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
    seen_norm_prices = {}
    seen_cache_keys = set()
    seen_db_ids = set()
    crawl_incomplete = False
    nav_locked = False

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

        await _telethon_nav_lock.acquire()
        nav_locked = True
        bot = await client.get_entity(TARGET_BOT)
        cat_msg: Any = await open_catalog(client, bot)
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

            page_norm_counts = {}
            for pb_text, _ in prod_buttons:
                pnk = norm_name(parse_catalog_button(pb_text)["name"])
                page_norm_counts[pnk] = page_norm_counts.get(pnk, 0) + 1

            for idx, (b_text, b_data) in enumerate(prod_buttons, 1):
                btn_info = parse_catalog_button(b_text)
                btn_name = btn_info["name"]
                live_price = btn_info["price_usd"]
                live_stock = btn_info["stock"]
                norm_k = norm_name(btn_name)
                seen_norms.add(norm_k)
                price_cents = int(round(live_price * 100)) if (live_price is not None and live_price > 0) else 0
                prev_price_set = seen_norm_prices.setdefault(norm_k, set())
                if price_cents > 0:
                    prev_price_set.add(price_cents)
                is_multi_price = bool(page_norm_counts.get(norm_k, 1) > 1 or len(prev_price_set) > 1)
                cache_k = f"{norm_k}_{price_cents}" if (is_multi_price and price_cents > 0 and norm_k in details_cache and int(round((details_cache[norm_k].get("price_usd") or 0) * 100)) != price_cents) else norm_k

                cached = details_cache.get(cache_k)
                cached_desc = (cached or {}).get("description", "")
                if (
                    cached is not None
                    and not force_refresh_all
                    and cached_desc
                    and len(cached_desc) > 30
                    and not is_menu_screen_text(cached_desc)
                    and not cached.get("needs_refresh")
                    and ((live_price is not None and live_price > 0) or (cached.get("price_usd") and float(cached.get("price_usd")) > 0))
                ):
                    eff_price = live_price if (live_price is not None and live_price > 0) else cached.get("price_usd")
                    eff_stock = live_stock
                    cached["stock"] = eff_stock
                    if eff_price is not None and eff_price > 0:
                        cached["price_usd"] = eff_price
                    cached["raw_btn"] = b_text
                    cached["page"] = page_indicator
                    pid, is_new = update_db_product(
                        btn_name, cached_desc, eff_stock, eff_price, db_path, allow_multi_price=is_multi_price
                    )
                    seen_cache_keys.add(cache_k)
                    if pid:
                        seen_db_ids.add(pid)
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
                        if not cat_msg:
                            cat_msg = await client.get_messages(bot, ids=cat_id)
                        if not cat_msg:
                            logger.warning("Catalog message unavailable for [%s] on attempt %d", btn_name, attempt + 1)
                            await asyncio.sleep(1.5)
                            continue
                        before_detail_sig = _button_signature(cat_msg)
                        if b_data:
                            await cat_msg.click(data=b_data)
                        else:
                            await cat_msg.click(text=b_text)
                        detail_msg = await _wait_for_menu_transition(
                            client, bot, cat_msg, before_detail_sig, timeout=6.0, in_place_only=True
                        )
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

                        details_cache[cache_k] = parsed
                        _persist_cache(details_cache, cache_path)

                        pid, is_new = update_db_product(
                            parsed["item_name"],
                            parsed["description"],
                            parsed["stock"],
                            parsed.get("price_usd"),
                            db_path,
                            allow_multi_price=is_multi_price,
                        )
                        seen_cache_keys.add(cache_k)
                        if pid:
                            seen_db_ids.add(pid)
                        if is_new:
                            new_count += 1
                        elif pid:
                            updated_count += 1

                        logger.info("-> OK: '%s' | Stock: %d pcs", parsed["item_name"], parsed["stock"])

                        back_btn = _find_button(detail_msg, "back")
                        if back_btn:
                            before_back_sig = _button_signature(detail_msg)
                            await _click_inline_button(detail_msg, back_btn)
                            returned_msg = await _wait_for_menu_transition(
                                client, bot, detail_msg, before_back_sig, timeout=6.0, in_place_only=True
                            )
                        else:
                            returned_msg = detail_msg
                        cat_msg = await _recover_to_catalog_page(client, bot, cat_id, returned_msg)
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
                    pid, is_new = update_db_product(
                        btn_name, fallback_desc, live_stock, live_price, db_path, allow_multi_price=is_multi_price
                    )
                    seen_cache_keys.add(cache_k)
                    if pid:
                        seen_db_ids.add(pid)
                    if is_new:
                        new_count += 1
                    elif pid:
                        updated_count += 1

            # Advance to next page
            cat_msg = await client.get_messages(bot, ids=cat_id)
            if not cat_msg or not _has_catalog_pagination(cat_msg):
                cat_msg = await _recover_to_catalog_page(client, bot, cat_id, cat_msg)
            if not cat_msg:
                if current_page < total_pages:
                    logger.warning("Lost catalog message on page %d/%d; stopping crawl.", current_page, total_pages)
                    crawl_incomplete = True
                break
            cat_id = cat_msg.id
            prev_page_num = _extract_page_num(cat_msg) or current_page
            next_btn = _find_button_any(cat_msg, ("▶️", "➡️", "▶", "➡"))

            if current_page < total_pages and next_btn:
                advanced = False
                for nav_attempt in range(2):
                    try:
                        before_sig = _button_signature(cat_msg)
                        await _click_inline_button(cat_msg, next_btn)
                        # Pagination edits the catalog message in place; ignore concurrent broadcast alerts
                        next_msg = await _wait_for_menu_transition(
                            client, bot, cat_msg, before_sig, timeout=8.0, in_place_only=True
                        )
                        next_page_num = _extract_page_num(next_msg)
                        if next_msg and _button_signature(next_msg) != before_sig and (
                            next_page_num is None or next_page_num > prev_page_num
                        ):
                            cat_msg = next_msg
                            cat_id = cat_msg.id
                            current_page += 1
                            advanced = True
                            break
                        logger.warning(
                            "Pagination stayed on page %s (attempt %d/2); retrying...",
                            prev_page_num, nav_attempt + 1,
                        )
                        next_btn = _find_button_any(cat_msg, ("▶️", "➡️", "▶", "➡")) or next_btn
                    except errors.FloodWaitError as fe:
                        logger.warning("Telegram FloodWait on pagination: sleeping %ds...", fe.seconds + 2)
                        await asyncio.sleep(fe.seconds + 2)
                    except Exception as e:
                        logger.error("Failed to advance to page %d (attempt %d): %s", current_page + 1, nav_attempt + 1, e)
                if not advanced:
                    logger.error("Pagination stalled on page %d/%d; stopping crawl.", current_page, total_pages)
                    crawl_incomplete = True
                    break
            else:
                if current_page < total_pages:
                    logger.warning("Missing Next button on page %d/%d; stopping crawl.", current_page, total_pages)
                    crawl_incomplete = True
                break

        if not crawl_incomplete and current_page >= total_pages:
            prune_stale_retail_catalog(
                details_cache, seen_cache_keys, seen_norms, seen_db_ids, db_path=db_path, min_seen=50
            )

        try:
            _persist_cache(details_cache, cache_path)
            if not crawl_incomplete and current_page >= total_pages:
                update_retail_catalogs(details_cache)
        except Exception as e:
            logger.warning("Could not persist scraped details: %s", e)

        return {
            "success": True,
            "total_scraped": len(details_cache),
            "new_items": new_count,
            "updated_items": updated_count,
            "pages_scanned": current_page,
            "total_pages": total_pages,
            "incomplete": crawl_incomplete,
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
            "total_pages": total_pages,
            "incomplete": True,
        }
    finally:
        if nav_locked and _telethon_nav_lock.locked():
            _telethon_nav_lock.release()
        if own_client and client:
            try:
                disc = client.disconnect()
                if inspect.isawaitable(disc):
                    await disc
            except Exception:
                pass


if __name__ == "__main__":
    asyncio.run(run_retail_crawler())
