"""
Script to generate executive Excel spreadsheet of all products from Supplier API and Bot catalog.
"""
import asyncio
import csv
import os
import re
import sqlite3
import subprocess
import sys
from typing import Optional
from dotenv import load_dotenv

load_dotenv()
from reseller import SupplierRegistry

HAS_OPENPYXL = False
try:
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    HAS_OPENPYXL = True
except ModuleNotFoundError:
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "openpyxl>=3.1.2"], check=True, capture_output=True, timeout=60)
        import openpyxl
        from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
        from openpyxl.utils import get_column_letter
        HAS_OPENPYXL = True
    except Exception:
        HAS_OPENPYXL = False


BRAND_SERVICE_KEYWORDS = (
    (("chatgpt", "gpt", "openai", "sora"), "OpenAI / ChatGPT"),
    (("claude", "anthropic"), "Anthropic / Claude"),
    (("cursor",), "Cursor AI"),
    (("gemini", "google"), "Google / Gemini"),
    (("midjourney",), "Midjourney"),
    (("grok", "xai"), "xAI / Grok"),
    (("deepseek",), "DeepSeek"),
    (("perplexity",), "Perplexity AI"),
    (("canva",), "Canva"),
    (("adobe",), "Adobe Creative Cloud"),
    (("capcut", "bytedance"), "CapCut Pro"),
    (("telegram",), "Telegram Premium"),
    (("nordvpn", "surfshark", "expressvpn", "vpn", "proxy"), "VPN & Proxy Services"),
    (("spotify", "netflix", "youtube", "apple music", "disney", "prime"), "Media & Streaming"),
    (("microsoft", "office", "windows", "azure", "onedrive"), "Microsoft Office / 365"),
    (("autodesk",), "Autodesk"),
    (("github", "copilot"), "GitHub Copilot"),
    (("codex",), "Codex API"),
    (("higgsfield",), "Higgsfield AI"),
    (("lovable",), "Lovable AI"),
    (("elevenlabs",), "ElevenLabs AI Voice"),
    (("kimi", "moonshot", "glm"), "Moonshot / Kimi / GLM"),
    (("suno",), "Suno AI Music"),
    (("luma",), "Luma Dream Machine"),
    (("runway",), "Runway Gen-3"),
    (("muse",), "Muse AI"),
)

EXCEL_CATEGORY_KEYWORDS = (
    (("cursor",), "Cursor API"),
    (("codex",), "Codex API"),
    (("chatgpt",), "ChatGPT"),
    (("claude",), "Claude"),
    (("gemini", "google", "antigravity", "argon", "hydrogen"), "Gemini"),
    (("vpn", "proxy"), "VPN & Security"),
    (("canva", "adobe", "capcut", "midjourney"), "Creative & Design"),
    (("office", "microsoft", "windows"), "Productivity & Office"),
    (("spotify", "netflix", "youtube"), "Streaming & Entertainment"),
)


def determine_brand_service(name: str) -> str:
    n = name.lower()
    for keywords, brand in BRAND_SERVICE_KEYWORDS:
        if any(k in n for k in keywords):
            return brand
    return "AI & Cloud Tools"


def determine_category(name: str, db_cat: str = "") -> str:
    if db_cat and db_cat != "AI Tools":
        return db_cat
    n = name.lower()
    for keywords, category in EXCEL_CATEGORY_KEYWORDS:
        if any(k in n for k in keywords):
            return category
    return db_cat or "AI Tools"


def parse_product_desc(name: str, desc: str):
    desc = desc or ""
    lines = [l.strip() for l in desc.splitlines() if l.strip()]

    # Delivery format
    m_deliv = re.search(r"Delivery format:?\s*([^\n\r]+)", desc, re.IGNORECASE)
    if m_deliv:
        deliv_fmt = m_deliv.group(1).strip()
    elif any(k in desc.lower() for k in ["email | password", "email | pass", "2fa"]):
        deliv_fmt = "Email | Password | 2FA"
    elif "api key" in name.lower() or "tokens" in name.lower() or "credits" in name.lower():
        deliv_fmt = "API Key / Token Secret"
    elif "invite" in name.lower() or "family" in name.lower():
        deliv_fmt = "Activation / Invitation Link"
    elif "key" in name.lower() or "license" in name.lower() or "code" in name.lower():
        deliv_fmt = "License Key / Redemption Code"
    else:
        deliv_fmt = "Account Credentials (Email | Password)"

    # Warranty policy
    m_war = re.search(r"((?:No warranty|Warranty:?|Replacement:?)[^\n\r]+)", desc, re.IGNORECASE)
    if m_war:
        warranty = m_war.group(1).strip()
    elif "no warranty" in desc.lower():
        warranty = "No warranty. Delivered as-is; guaranteed active upon initial receipt."
    elif any(k in name.lower() for k in ["1 month", "1m", "30d"]):
        warranty = "24-hour verification & replacement warranty; valid for 30-day billing cycle."
    elif any(k in name.lower() for k in ["1 year", "12m", "12 months"]):
        warranty = "30-day supplier replacement warranty; 12-month subscription period."
    else:
        warranty = "24-hour verification & replacement warranty upon delivery."

    # Important notes and precautions
    m_prec = re.search(r"((?:IMPORTANT|Important information|Notice|CAUTION|Note):?[\s\S]*?)(?=(?:\n[A-Z\s]{4,}:|\Z))", desc, re.IGNORECASE)
    if m_prec:
        precautions = m_prec.group(1).strip()
    elif "chatgpt" in name.lower():
        precautions = (
            "IMPORTANT HANDLING NOTES:\n"
            "1. First, navigate to Settings -> Account -> Email and change the linked email to your personal address.\n"
            "2. Only after updating the email address should you change the password or 2FA authenticator.\n"
            "3. Do not use blacklisted or free public VPN IPs during initial login to prevent automated security flags."
        )
    elif "api" in name.lower():
        precautions = (
            "CRITICAL API SECURITY:\n"
            "1. Never commit API keys to public repositories or client-side code.\n"
            "2. Observe rate limits and token quotas to prevent temporary rate limiting.\n"
            "3. Rotate credentials immediately if any unauthorized exposure is suspected."
        )
    else:
        precautions = (
            "CRITICAL PRECAUTIONS:\n"
            "1. Test and verify login access within 24 hours of delivery.\n"
            "2. Enable personal two-factor authentication (2FA) where supported.\n"
            "3. Avoid simultaneous logins from multiple geographic locations."
        )

    # Setup instructions
    m_inst = re.search(r"((?:LOGIN INSTRUCTION|HOW TO LOG IN|HOW TO USE|INSTRUCTION|SETUP GUIDE)[\s\S]*?)(?=(?:\nIMPORTANT|\nNOTE|\nWAR|\Z))", desc, re.IGNORECASE)
    if m_inst:
        instructions = m_inst.group(1).strip()
    elif "chatgpt" in name.lower():
        instructions = (
            "STEP-BY-STEP LOGIN INSTRUCTIONS:\n"
            "1. Open the official ChatGPT web portal (https://chatgpt.com) or the official mobile app.\n"
            "2. Enter the delivered email and password.\n"
            "3. When prompted for 2FA, generate a 6-digit code using Google Authenticator or https://2fa.online with the delivered 2FA secret.\n"
            "4. Enter the 6-digit code into ChatGPT (refreshes every 30 seconds).\n"
            "5. Once logged in, bind your personal email in Account Settings."
        )
    elif "api" in name.lower():
        instructions = (
            "API INTEGRATION INSTRUCTIONS:\n"
            "1. Copy the delivered API key / endpoint credentials.\n"
            "2. Set the key in your application environment variable (e.g., OPENAI_API_KEY or BASE_URL).\n"
            "3. Point your HTTP client to the designated endpoint and verify connection with a test ping."
        )
    else:
        instructions = (
            "ACTIVATION INSTRUCTIONS:\n"
            "1. Access the official service portal.\n"
            "2. Log in using the provided credentials or apply the delivered redemption token.\n"
            "3. Confirm premium status activation in your account billing overview."
        )

    # Overview and features
    m_desc = re.search(r"Description:?\s*([^\n\r]+(?:\n[^\n\r]+)?)", desc, re.IGNORECASE)
    if m_desc:
        overview = m_desc.group(1).strip()
    else:
        clean_lines = [l for l in lines if not l.lower().startswith(("item:", "delivery format", "payment method", "no warranty", "login instruction", "how to"))]
        overview = clean_lines[0] if clean_lines else f"Premium digital access for {name}."

    return deliv_fmt, instructions, overview, warranty, precautions


def get_db_path(custom_path: Optional[str] = None) -> str:
    if custom_path and os.path.exists(custom_path):
        return custom_path
    try:
        from bot import DB_PATH
        if DB_PATH and (DB_PATH == ":memory:" or os.path.exists(DB_PATH)):
            return DB_PATH
    except Exception:
        pass
    db_env = os.environ.get("DB_PATH", "store.db")
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if db_env == ":memory:":
        return ":memory:"
    if db_env == "/data/store.db" and sys.platform == "win32" and not os.path.exists("/.dockerenv"):
        return os.path.join(base_dir, "store.db")
    if os.path.isabs(db_env) and os.path.exists(db_env):
        return db_env
    cand = os.path.join(base_dir, db_env)
    if os.path.exists(cand):
        return cand
    if os.path.exists("store.db"):
        return "store.db"
    return db_env


CATALOG_COLUMNS = [
    ("Product ID", 12, "center"),
    ("Product Name", 36, "left"),
    ("Category", 20, "left"),
    ("Brand / Service", 24, "left"),
    ("Supplier Cost (USD)", 18, "right"),
    ("Store Price (USD)", 18, "right"),
    ("Profit Margin ($)", 18, "right"),
    ("Margin (%)", 14, "right"),
    ("Stock Status", 20, "center"),
    ("Supplier ID", 14, "center"),
    ("Fulfillment Type", 22, "center"),
    ("Delivery Format", 26, "left"),
    ("Full Step-by-Step Instructions", 50, "left"),
    ("Product Overview & Features", 40, "left"),
    ("Warranty & Policy", 34, "left"),
    ("Critical Handling Notes & Precautions", 48, "left"),
]


async def build_catalog_excel(target_db_path: Optional[str] = None):
    columns = CATALOG_COLUMNS
    active_db = get_db_path(target_db_path)
    conn = sqlite3.connect(active_db)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()

    table_exists = c.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='products'").fetchone()[0]
    if not table_exists:
        base_store = os.path.join(os.path.dirname(os.path.abspath(__file__)), "store.db")
        if os.path.exists(base_store) and base_store != active_db:
            conn.close()
            active_db = base_store
            conn = sqlite3.connect(active_db)
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            table_exists = c.execute("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='products'").fetchone()[0]

    all_db_rows = []
    if table_exists:
        all_db_rows = [dict(r) for r in c.execute("SELECT * FROM products ORDER BY id ASC").fetchall()]

    conn.close()

    # Fetch live supplier products for latest live stock/pricing
    supplier_map = {}
    try:
        registry = SupplierRegistry.from_env()
        client = registry.get()
        if client and client.api_key:
            sup_list = await client.get_products()
            for sp in sup_list:
                supplier_map[str(sp["id"])] = sp
        await registry.close_all()
    except Exception as e:
        print(f"[Warning] Live supplier catalog fetch unavailable ({e}), using store catalog.")

    if not HAS_OPENPYXL:
        out_file = "CreditLog_Bot_Supplier_Products_Catalog.csv"
        with open(out_file, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow([c[0] for c in columns])
            for rd in all_db_rows:
                sname = rd["name"]
                cat_name = rd.get("category") or determine_category(sname)
                brand_svc = determine_brand_service(sname)
                sup_id = str(rd["supplier_product_id"]) if rd.get("supplier_product_id") else ""
                live_sp = supplier_map.get(sup_id, {})

                cost_usd = live_sp.get("price_usd") or (((rd.get("supplier_price") or 0) / 100.0) if rd.get("supplier_price") else round(((rd.get("price") or 0) * 0.65) / 100.0, 2))
                store_price = (rd.get("price") or 0) / 100.0
                margin_usd = round(store_price - cost_usd, 2)
                margin_pct = f"{round((margin_usd / cost_usd) * 100)}%" if cost_usd > 0 else "0%"

                stock_val = live_sp.get("stock") if "stock" in live_sp else rd.get("supplier_stock")
                if stock_val is not None and stock_val > 0:
                    stock_str = f"In Stock ({stock_val})"
                elif stock_val == 0:
                    stock_str = "Out of Stock"
                else:
                    stock_str = "Available on Order" if sup_id else "Active Store Item"

                deliv_type = "Auto-Fulfillment (API)" if (rd.get("auto_fulfill") and sup_id) else "Manual Delivery"
                deliv_fmt, inst, overview, war, prec = parse_product_desc(sname, rd.get("description", ""))
                writer.writerow([
                    rd["id"], sname, cat_name, brand_svc,
                    f"${cost_usd:.2f}", f"${store_price:.2f}", f"${margin_usd:.2f}", margin_pct,
                    stock_str, sup_id, deliv_type, deliv_fmt, inst, overview, war, prec
                ])
        print(f"Catalog fallback CSV saved to {out_file} (openpyxl not installed).")
        return out_file

    wb = openpyxl.Workbook()

    # Executive styling palette
    header_fill = PatternFill(start_color="0F172A", end_color="0F172A", fill_type="solid")
    header_font = Font(name="Calibri", size=11, bold=True, color="FFFFFF")
    header_align = Alignment(horizontal="center", vertical="center", wrap_text=True)

    zebra_fill = PatternFill(start_color="F8FAFC", end_color="F8FAFC", fill_type="solid")
    white_fill = PatternFill(start_color="FFFFFF", end_color="FFFFFF", fill_type="solid")

    in_stock_fill = PatternFill(start_color="DCFCE7", end_color="DCFCE7", fill_type="solid")
    in_stock_font = Font(name="Calibri", size=10, bold=True, color="166534")

    out_stock_fill = PatternFill(start_color="FEE2E2", end_color="FEE2E2", fill_type="solid")
    out_stock_font = Font(name="Calibri", size=10, bold=True, color="991B1B")

    avail_fill = PatternFill(start_color="FEF3C7", end_color="FEF3C7", fill_type="solid")
    avail_font = Font(name="Calibri", size=10, bold=True, color="92400E")

    currency_font = Font(name="Calibri", size=10, bold=True, color="047857")
    margin_fill = PatternFill(start_color="ECFDF5", end_color="ECFDF5", fill_type="solid")
    regular_font = Font(name="Calibri", size=10, color="1E293B")
    bold_font = Font(name="Calibri", size=10, bold=True, color="0F172A")
    id_font = Font(name="Calibri", size=10, bold=True, color="0F172A")

    thin_border = Border(
        left=Side(style="thin", color="E2E8F0"),
        right=Side(style="thin", color="E2E8F0"),
        top=Side(style="thin", color="E2E8F0"),
        bottom=Side(style="thin", color="E2E8F0"),
    )

    # Sheet 1: Master Store Catalog (sorted 1..N)
    ws1 = wb.active
    ws1.title = "Products Catalog"
    ws1.views.sheetView[0].showGridLines = True

    for col_idx, (col_name, col_width, col_align) in enumerate(columns, 1):
        cell = ws1.cell(row=1, column=col_idx, value=col_name)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align
        cell.border = thin_border
        col_letter = get_column_letter(col_idx)
        ws1.column_dimensions[col_letter].width = col_width

    ws1.row_dimensions[1].height = 28
    ws1.freeze_panes = "C2"  # Freeze Product ID and Product Name

    category_stats = {}

    for row_idx, rd in enumerate(all_db_rows, 2):
        sname = rd["name"]
        cat_name = rd.get("category") or determine_category(sname)
        brand_svc = determine_brand_service(sname)
        sup_id = str(rd["supplier_product_id"]) if rd.get("supplier_product_id") else ""
        live_sp = supplier_map.get(sup_id, {})

        cost_usd = live_sp.get("price_usd") or (((rd.get("supplier_price") or 0) / 100.0) if rd.get("supplier_price") else round(((rd.get("price") or 0) * 0.65) / 100.0, 2))
        store_price = (rd.get("price") or 0) / 100.0
        margin_usd = round(store_price - cost_usd, 2)
        margin_pct = (margin_usd / cost_usd) if cost_usd > 0 else 0.0

        stock_val = live_sp.get("stock") if "stock" in live_sp else rd.get("supplier_stock")
        is_in_stock = False
        is_oos = False
        if stock_val is not None and stock_val > 0:
            stock_str = f"In Stock ({stock_val})"
            stock_style = (in_stock_fill, in_stock_font)
            is_in_stock = True
        elif stock_val == 0:
            stock_str = "Out of Stock"
            stock_style = (out_stock_fill, out_stock_font)
            is_oos = True
        else:
            stock_str = "Available on Order" if sup_id else "Active Store Item"
            stock_style = (avail_fill, avail_font)
            is_in_stock = True

        deliv_type = "Auto-Fulfillment (API)" if (rd.get("auto_fulfill") and sup_id) else "Manual Delivery"
        deliv_fmt, inst, overview, war, prec = parse_product_desc(sname, rd.get("description", ""))

        row_fill = zebra_fill if (row_idx % 2 == 0) else white_fill
        values = [
            rd["id"],
            sname,
            cat_name,
            brand_svc,
            cost_usd,
            store_price,
            margin_usd,
            margin_pct,
            stock_str,
            sup_id,
            deliv_type,
            deliv_fmt,
            inst,
            overview,
            war,
            prec,
        ]

        for col_idx, val in enumerate(values, 1):
            cell = ws1.cell(row=row_idx, column=col_idx, value=val)
            cell.border = thin_border
            cell.fill = row_fill
            cell.font = regular_font

            align_mode = columns[col_idx - 1][2]
            wrap = col_idx in [12, 13, 14, 15, 16]
            cell.alignment = Alignment(horizontal=align_mode, vertical="top", wrap_text=wrap)

            if col_idx == 1:
                cell.font = id_font
            elif col_idx == 2:
                cell.font = bold_font
            elif col_idx in [5, 6]:
                cell.number_format = "$#,##0.00"
                cell.font = bold_font
            elif col_idx == 7:
                cell.number_format = "$#,##0.00"
                cell.font = currency_font
                cell.fill = margin_fill
            elif col_idx == 8:
                cell.number_format = "0%"
                cell.font = currency_font
            elif col_idx == 9:
                cell.fill = stock_style[0]
                cell.font = stock_style[1]

        ws1.row_dimensions[row_idx].height = 40

        # Accumulate category stats
        stats = category_stats.setdefault(cat_name, {
            "total": 0, "in_stock": 0, "out_of_stock": 0,
            "cost_sum": 0.0, "price_sum": 0.0, "margin_sum": 0.0
        })
        stats["total"] += 1
        if is_in_stock:
            stats["in_stock"] += 1
        if is_oos:
            stats["out_of_stock"] += 1
        stats["cost_sum"] += cost_usd
        stats["price_sum"] += store_price
        stats["margin_sum"] += margin_usd

    if all_db_rows:
        ws1.auto_filter.ref = f"A1:P{len(all_db_rows)+1}"

    # Sheet 2: Category & Profit Summary
    ws2 = wb.create_sheet(title="Category & Profit Summary")
    ws2.views.sheetView[0].showGridLines = True

    sum_cols = [
        ("Category", 26, "left"),
        ("Total Products", 16, "center"),
        ("In-Stock", 14, "center"),
        ("Out of Stock", 14, "center"),
        ("Avg Cost (USD)", 18, "right"),
        ("Avg Price (USD)", 18, "right"),
        ("Avg Margin ($)", 18, "right"),
        ("Avg Margin (%)", 16, "right"),
    ]

    for col_idx, (col_name, col_width, col_align) in enumerate(sum_cols, 1):
        cell = ws2.cell(row=1, column=col_idx, value=col_name)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = header_align
        cell.border = thin_border
        col_letter = get_column_letter(col_idx)
        ws2.column_dimensions[col_letter].width = col_width

    ws2.row_dimensions[1].height = 28

    tot_prods = 0
    tot_instock = 0
    tot_oos = 0
    tot_cost = 0.0
    tot_price = 0.0
    tot_margin = 0.0

    cur_row = 2
    for cat_name, st in sorted(category_stats.items()):
        cnt = st["total"]
        avg_cost = st["cost_sum"] / cnt if cnt > 0 else 0.0
        avg_price = st["price_sum"] / cnt if cnt > 0 else 0.0
        avg_margin = st["margin_sum"] / cnt if cnt > 0 else 0.0
        avg_pct = avg_margin / avg_cost if avg_cost > 0 else 0.0

        tot_prods += cnt
        tot_instock += st["in_stock"]
        tot_oos += st["out_of_stock"]
        tot_cost += st["cost_sum"]
        tot_price += st["price_sum"]
        tot_margin += st["margin_sum"]

        r_fill = zebra_fill if (cur_row % 2 == 0) else white_fill
        row_vals = [
            cat_name,
            cnt,
            st["in_stock"],
            st["out_of_stock"],
            avg_cost,
            avg_price,
            avg_margin,
            avg_pct,
        ]

        for col_idx, val in enumerate(row_vals, 1):
            cell = ws2.cell(row=cur_row, column=col_idx, value=val)
            cell.border = thin_border
            cell.fill = r_fill
            cell.font = regular_font
            cell.alignment = Alignment(horizontal=sum_cols[col_idx-1][2], vertical="center")

            if col_idx == 1:
                cell.font = bold_font
            elif col_idx in [5, 6]:
                cell.number_format = "$#,##0.00"
                cell.font = bold_font
            elif col_idx == 7:
                cell.number_format = "$#,##0.00"
                cell.font = currency_font
                cell.fill = margin_fill
            elif col_idx == 8:
                cell.number_format = "0%"
                cell.font = currency_font

        ws2.row_dimensions[cur_row].height = 24
        cur_row += 1

    # Totals Row
    avg_tot_cost = tot_cost / tot_prods if tot_prods > 0 else 0.0
    avg_tot_price = tot_price / tot_prods if tot_prods > 0 else 0.0
    avg_tot_margin = tot_margin / tot_prods if tot_prods > 0 else 0.0
    avg_tot_pct = avg_tot_margin / avg_tot_cost if avg_tot_cost > 0 else 0.0

    total_fill = PatternFill(start_color="F1F5F9", end_color="F1F5F9", fill_type="solid")
    thick_top_border = Border(
        left=Side(style="thin", color="CBD5E1"),
        right=Side(style="thin", color="CBD5E1"),
        top=Side(style="medium", color="0F172A"),
        bottom=Side(style="double", color="0F172A"),
    )

    totals_vals = [
        "TOTAL / STORE AVERAGE",
        tot_prods,
        tot_instock,
        tot_oos,
        avg_tot_cost,
        avg_tot_price,
        avg_tot_margin,
        avg_tot_pct,
    ]

    for col_idx, val in enumerate(totals_vals, 1):
        cell = ws2.cell(row=cur_row, column=col_idx, value=val)
        cell.fill = total_fill
        cell.border = thick_top_border
        cell.font = bold_font
        cell.alignment = Alignment(horizontal=sum_cols[col_idx-1][2], vertical="center")
        if col_idx in [5, 6, 7]:
            cell.number_format = "$#,##0.00"
        elif col_idx == 8:
            cell.number_format = "0%"

    ws2.row_dimensions[cur_row].height = 28

    out_file = "CreditLog_Bot_Supplier_Products_Catalog.xlsx"
    try:
        wb.save(out_file)
    except PermissionError:
        import time
        out_file = f"CreditLog_Bot_Supplier_Products_Catalog_{int(time.time())}.xlsx"
        wb.save(out_file)
    print(f"Excel catalog successfully saved to {out_file} with {len(all_db_rows)} products.")
    return out_file


if __name__ == "__main__":
    asyncio.run(build_catalog_excel())
