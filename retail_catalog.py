"""Supplier retail catalog items missing from automated reseller API feed."""

import json
import os
import re

# (name, cost_usd, is_in_stock)
RETAIL_CATALOG_ITEMS = [
    ('100$ API Claude 30D', 2.50, True),
    ('100$ Claude API 30D FableAI', 2.50, True),
    ('500$ API Claude 30D', 7.50, True),
    ('500$ Claude API 30D FableAI', 7.70, True),
    ('AdGuard VPN 7D', 2.30, False),
    ('Admin Netflix 4K 1M', 3.80, False),
    ('Adobe All Apps 7D', 3.67, False),
    ('Adobe CC Pro 1M', 18.17, False),
    ('Adobe Express 1 Year', 0.50, True),
    ('Adobe Express 12M Account', 1.50, True),
    ('Adobe Express 4M Trial', 1.00, True),
    ('Adobe Full App Bypass Version 1M', 5.50, False),
    ('Airtable Team 12 Months', 7.00, True),
    ('Amazon Music 3 Months', 2.00, False),
    ('Amazon prime 1 month', 1.50, False),
    ('Amazon Prime Video 1 Month (Video Only)', 1.50, False),
    ('AntiGravity Argon/Hydro 3M Solo FAM', 7.00, True),
    ('AntiGravity Argon/Hydrogen 3M FAM', 4.00, False),
    ('API Codex Astra $10 1M', 2.15, True),
    ('API Codex Astra $100 1M', 9.08, True),
    ('API Codex Astra $1000 1M', 60.48, True),
    ('API Codex Astra $200 1M', 13.00, True),
    ('API Codex Astra $30 1M', 4.53, True),
    ('API Codex Astra $300 1M', 18.15, True),
    ('API Codex Astra $50 1M', 5.53, True),
    ('API Codex Astra $500 1M', 30.26, True),
    ('API Kimi 100M Tokens 7D', 6.00, True),
    ('API Kimi 15M Tokens 1D', 3.00, True),
    ('API Kimi 300M Tokens 30D', 10.00, True),
    ('API Qwen $10 1 Month', 2.20, True),
    ('API Qwen $100 1 Month', 6.50, True),
    ('API Qwen $30 1 Month', 2.78, True),
    ('API Qwen $50 1 Month', 4.00, True),
    ('API QWEN Unlimited Tokens 7D', 5.00, True),
    ('Apple Music 5M', 1.60, True),
    ('Apple Music 6M', 2.00, False),
    ('AutoCAD EDU 12M', 0.00, False),
    ('Autodesk 1 App 3Y Upgrade', 5.00, False),
    ('Autodesk Admin Access', 11.50, True),
    ('Autodesk All Apps 3Y', 3.80, True),
    ('Autodesk Full Apps 3Y Upgrade', 8.00, False),
    ('Autodesk Panel 250 Users', 3.90, True),
    ('Avira VPN 3 Months', 2.50, True),
    ('Canva Pro Add to Team 12 Months', 4.50, True),
    ('Canva Pro Admin 500 Invitations', 7.00, True),
    ('Canva Pro Lite 1 Year', 2.99, False),
    ('CapCut Pro 1 Month', 2.10, True),
    ('CapCut Pro 1M 1200 Credits', 1.92, False),
    ('CapCut Pro 1M 1600 AI Credits', 2.70, False),
    ('CapCut Pro 6 Months', 9.99, True),
    ('CapCut Pro 6-7 Months Personal', 14.00, True),
    ('CapCut Pro 7 Days', 0.65, True),
    ('CapCut Pro 7D 100-500 Credits', 0.50, True),
    ('CapCut Pro 7D 1200 Credits', 0.90, True),
    ('CapCut Pro 7D 500 Credits', 0.75, True),
    ('CapCut Pro Professional 28 Days', 1.80, True),
    ('CapCut Pro Team 1M', 2.20, False),
    ('CapCut Pro Team 1M 1200 Credits', 2.80, False),
    ('CapCut Pro Team 3M', 7.90, False),
    ('CDK Grok Super 1M', 22.50, False),
    ('CDK Heavy Grok 1M', 34.99, False),
    ('CDK Super Grok Old 1M', 26.50, False),
    ('CDK Super Grok Old 3M', 46.00, False),
    ('CDK Super Grok Old 7D', 6.50, False),
    ('ChatGPT Business 1M', 5.80, False),
    ('ChatGPT Business Team 1M', 17.00, False),
    ('ChatGPT Business Team 1M Invite', 18.50, True),
    ('ChatGPT Go 1M', 4.20, False),
    ('ChatGPT Go 3 months', 4.40, True),
    ('ChatGPT Plus 1M Apple Pay', 5.90, True),
    ('ChatGPT Plus 1M GGPAY', 5.90, True),
    ('ChatGPT Plus 1M Google Pay', 6.80, True),
    ('ChatGPT Plus 1M Pix', 5.99, True),
    ('ChatGPT Plus 1M UPI', 4.70, False),
    ('ChatGPT Plus 4M', 36.17, False),
    ('ChatGPT Plus GGPay 30D', 7.91, False),
    ('ChatGPT Plus Pix 30D', 7.30, False),
    ('ChatGPT Plus Your Mail 1M', 20.04, False),
    ('ChatGPT Plus Your Mail 4M', 43.00, False),
    ('Claude API 100M Tokens 1 Day', 6.00, True),
    ('Claude API 100M Tokens 3 Days', 7.50, True),
    ('Claude API 100M Tokens 3D', 4.90, True),
    ('Claude API 10M Tokens 1 Day', 2.00, True),
    ('Claude API 10M Tokens 1D', 3.00, True),
    ('Claude API 500M Tokens 1 Day', 20.00, False),
    ('Claude API 500M Tokens 6D', 23.00, True),
    ('Claude API 50M Tokens 1 Day', 5.00, True),
    ('Claude API 50M Tokens 2 Days', 4.00, True),
    ('Claude API 50M Tokens 3 Days', 6.00, False),
    ('Codex API 100M Tokens 1 Day', 5.00, True),
    ('Codex API 100M Tokens 3D', 6.00, True),
    ('Codex API 10M Tokens 1 Day', 0.00, False),
    ('Codex API 10M Tokens 1D', 0.00, False),
    ('Codex API 500M Tokens 6 Days', 29.80, True),
    ('Codex API 500M Tokens 6D', 20.00, False),
    ('Codex API 50M Tokens 1 Day', 4.20, True),
    ('Codex API 50M Tokens 2D', 4.00, True),
    ('Coursera Premium 12 Months', 3.60, True),
    ('Cursor Pro API 2600 Credits 1 Month', 11.50, True),
    ('Cursor Pro API 400 Credits/day 1 Month', 15.50, True),
    ('Cursor Pro API 6500 Credits 1 Month', 15.50, True),
    ('Customer.io Essentials 1Y', 4.50, False),
    ('DeepSeek API $10 1 Month', 2.20, True),
    ('DeepSeek API $100 1 Month', 6.10, True),
    ('DeepSeek API $30 1 Month', 2.90, True),
    ('DeepSeek API $50 1 Month', 4.10, True),
    ('Discord Decor 66K Profile Effect', 6.00, False),
    ('Discord Decor 79K Profile Effect', 6.50, False),
    ('Discord Decor 92K Profile Effect', 7.50, False),
    ('Discord Gifting Badge x1', 5.50, False),
    ('Discord Nitro 3M Token', 5.50, True),
    ('Discord Nitro Trial 3M', 7.50, False),
    ('Duolingo Super 12M', 1.50, True),
    ('Duolingo Super 2 Months Link', 2.50, True),
    ('Eleven Reader Ultra 12M', 2.50, False),
    ('ElevenLabs Creator 1M', 6.37, True),
    ('ElevenLabs Creator Account 3 Months', 8.50, True),
    ('ElevenLabs Free 10k Credits', 0.60, False),
    ('ElevenLabs Redeem 131K Credits', 0.00, False),
    ('ElevenLabs Redeem 1M Credits', 12.54, True),
    ('ElevenLabs Redeem 300K Credits', 6.50, True),
    ('ElevenLabs Redeem 3M Credits', 34.50, True),
    ('ElevenReader Ultra 12 Months', 0.00, False),
    ('ElevenReader Ultra 1Y Gift Link', 5.50, False),
    ('ExpressVPN 1 Month 5 Devices', 2.00, False),
    ('ExpressVPN 12M', 7.00, False),
    ('ExpressVPN 3 Days', 0.00, False),
    ('Fallout 76 MS Store', 2.48, False),
    ('Figma Pro EDU 12 Months', 7.50, True),
    ('Figma Pro EDU 2 Years', 3.50, True),
    ('Flux 03 1M', 1.50, False),
    ('Freepik Magnific API Panel 30D', 5.00, False),
    ('Gamma AI 20K Credits 10 Years', 11.00, False),
    ('Gamma AI Plus 1 Month', 10.00, False),
    ('Gamma Pro 12M Coupon Code', 46.00, False),
    ('Gamma Pro 1M', 14.00, True),
    ('Gemini API $10 1M', 2.50, True),
    ('Gemini API $30 1M', 3.50, True),
    ('Gemini API $50 1M', 5.00, True),
    ('Gemini Plus 12 Months', 1.30, False),
    ('Gemini Plus 6 Months [No card]', 0.90, True),
    ('Gemini Pro 12 Months FAM', 7.54, True),
    ('Gemini Pro 3 Month CDK', 2.90, False),
    ('Gemini Pro 3 Months FAM', 3.50, True),
    ('Gemini Pro 6 Months FAM', 5.16, True),
    ('GitHub Student Developer Pack', 11.00, True),
    ('Gmail account new', 0.62, False),
    ('Google AI Pro 12M CDK V2', 2.00, True),
    ('Grammarly AI Pro 1M', 5.50, True),
    ('Grammarly Pro 12 Months', 7.20, True),
    ('Grok + Cursor Premium 14 days', 6.99, False),
    ('Grok + Cursor Premium 14D', 5.99, True),
    ('Grok + Cursor Premium 1M', 12.74, False),
    ('Grok + Cursor Premium 1M (W7D)', 16.00, False),
    ('Grok + Cursor Ultra 1M', 68.50, True),
    ('Grok API $10 1M', 10.00, True),
    ('Grok API $100 1M', 100.00, True),
    ('Grok API $30 1M', 30.00, True),
    ('Grok API $50 1M', 50.00, True),
    ('Grok API 4.5 HIGH/XHIGH 100M Tokens 7 Days', 5.00, True),
    ('Grok API 4.5 HIGH/XHIGH 15M Tokens 1 Day', 2.50, True),
    ('Grok Plus + Cursor 1 Month', 6.50, False),
    ('Grok Plus 1 Month', 5.00, False),
    ('Grok Plus 12M', 40.00, True),
    ('Grok Plus 7 Days', 3.00, False),
    ('Grok Super 1M', 33.50, True),
    ('Grok Super 1M (5 Days Warranty)', 38.50, False),
    ('HeyGen Creator 1M 28D', 25.00, False),
    ('HeyGen Creator 1M 48H', 19.23, True),
    ('Higgsfield Genjutsu 1M', 5.00, False),
    ('Higgsfield Starter 1M', 10.00, False),
    ('HMA Android/PC 20-30 Days', 1.00, True),
    ('HMA iOS 25-30 Days 4 Devices', 1.40, False),
    ('HMA VPN 20-30 Days', 2.50, False),
    ('HMA VPN 30 Days', 1.00, True),
    ('Hotspot Shield VPN 7D', 2.00, False),
    ('iCloud 2TB 3M', 11.50, False),
    ('iCloud 400GB Your Mail 12M', 24.00, True),
    ('iLovePDF Premium 1 Year', 0.00, False),
    ('IPVanish VPN 7D', 2.00, False),
    ('JetBrains EDU Pack 12 Months', 3.20, True),
    ('Kaspersky Premium + VPN 1Y', 9.60, True),
    ('Kimi GLM DeepSeek API 100M Tokens 4D', 5.00, True),
    ('Kimi GLM DeepSeek API 500M Tokens 7D', 14.00, True),
    ('Kimi GLM DeepSeek API 50M Tokens 2D', 2.70, True),
    ('Kiro Pro Edu Official Owner 1Y', 73.00, False),
    ('Kling 66 Credits', 0.55, False),
    ('Kling Pro 3K3 Credits 1M', 26.00, False),
    ('Kling Random 700-1000 Credits 1M', 8.50, False),
    ('Kling Standard 700-1000 Credits 1M', 6.00, True),
    ('KlingAI 725-850 Credits 1M', 10.00, False),
    ('KlingAI 750-1100 Credits 1M', 11.38, False),
    ('KREA Basic', 7.50, False),
    ('Krea Basic 5100 Credits 1M', 5.50, False),
    ('KREA Max', 36.50, True),
    ('Leonardo AI Essential 8500 Credits 1M', 3.00, False),
    ('Leonardo AI Video Gen', 2.90, False),
    ('LinkedIn Career 3M', 1.00, True),
    ('LinkedIn Premium 3-Month Code', 2.50, False),
    ('Lovable EDU 12 Months (75 Credits)', 4.50, False),
    ('Lovable Pro 12 Months', 45.00, False),
    ('Lovable Pro Lite 1 Year Link', 6.99, False),
    ('Lovable Pro Lite 12 months + 300 credits', 6.30, False),
    ('Loveable Pro 1M Redeem Link', 1.20, False),
    ('Manus Pro 1 Year Coupon', 58.00, False),
    ('McAfee AntiVirus 1 Device 1Y', 7.26, False),
    ('Microsoft 365 Family 5U 12M', 12.00, True),
    ('Microsoft Copilot Pro 1 Month', 6.00, True),
    ('Microsoft Office 365 Plus 1 Year', 1.60, True),
    ('Microsoft Office LTSC Professional Plus 2024 Key Global', 2.80, True),
    ('MiniMax Redeem 131K', 2.60, True),
    ('MiniMax Redeem 300K', 6.00, True),
    ('Mobbin 10x Seat 12M', 12.50, True),
    ('Muse AI 1 Billion Tokens', 0.50, False),
    ('Muse AI 1B Gmail Stable', 0.55, True),
    ('Muse AI 1B Tokens', 0.45, True),
    ('Muse AI 25-31B Tokens', 1.30, False),
    ('Muse AI 2B Tokens', 0.60, False),
    ('Muse AI 31 Billion Tokens', 0.99, False),
    ('Muse AI 31B Gmail Stable', 1.70, False),
    ('Muse AI Account 1B Tokens', 0.30, False),
    ('Muse AI Account 2B Tokens', 0.50, False),
    ('Muse AI Account 31B Tokens', 0.89, False),
    ('Netflix 4K Private Slot 1M', 4.10, True),
    ('Netflix Premium 4K Slot 1M', 5.22, True),
    ('NordVPN 10 Devices 3M', 11.00, False),
    ('NordVPN 10 Devices 6M', 16.16, False),
    ('NordVPN Basic 3 Months Code', 2.80, True),
    ('NordVPN Coupon 3 Months', 2.20, False),
    ('Notion Business 1 Year', 19.00, False),
    ('Notion Business 3M', 1.50, True),
    ('Notion Business 3M Coupon Code', 2.00, True),
    ('Notion Business 6M', 17.50, True),
    ('Notion Business Admin 1M', 7.70, False),
    ('O365 Ready Account Random 6-12M', 4.00, True),
    ('OpenArt Starter 4K Credits 1M', 13.84, False),
    ('PC Game Pass 1M', 8.36, False),
    ('Perplexity Pro 1M', 8.16, False),
    ('Picsart Pro 6 Months', 4.50, True),
    ('Pixverse 600 Credits', 1.00, False),
    ('Pocket FM Premium 3M', 0.60, False),
    ('Prime Video 6 Months', 2.20, True),
    ('Prime Video Ultra 6M 6 Profiles', 2.90, False),
    ('Proton Unlimited 1M', 5.50, False),
    ('Quizlet Plus 12M', 9.50, True),
    ('Quizlet Plus Ultimate 12M', 9.50, True),
    ('Readwise 1 Year', 5.50, True),
    ('Replit Core 1 Month + $20 Credits', 3.50, False),
    ('Scribd Premium 1M', 2.10, True),
    ('Scribd Premium 1M Instant', 1.20, True),
    ('Seedance 400-600 Credits', 1.50, False),
    ('Spotify Individual 12M', 22.00, False),
    ('Spotify Individual 3M Activation Link', 2.50, True),
    ('Spotify Premium 3M', 1.50, False),
    ('Spotify Premium 6 Months', 9.80, False),
    ('Spotify Premium Account 1M', 2.20, True),
    ('Steam Account', 1.60, True),
    ('Suno Pro $10 Plan 1 Month', 6.99, True),
    ('Suno Pro 2500 Credits 1 Month', 6.00, False),
    ('Super Duolingo Your Mail 12 Months', 9.00, True),
    ('Super Grok 7 Days', 3.75, False),
    ('Supercut Pro 10 Seats 12M', 6.00, True),
    ('Surfshark VPN 2 Months Coupon Code', 2.99, True),
    ('Surfshark VPN 5-7 Days 6 Devices', 0.90, True),
    ('Surfshark VPN One 7D', 2.00, True),
    ('Unity 1 Year', 2.60, True),
    ('Veo Ultra X20 0 Credits 1M', 17.50, False),
    ('Veo Ultra X20 25K Credits Share 1M', 23.00, True),
    ('Vidiq Boost 1 Channel 2000 Credits', 8.00, True),
    ('Warp Build 1 Year', 14.89, True),
    ('Windows 10 / 11 Pro Permanent Key', 5.00, True),
    ('Wordwall Pro 1 Month', 3.80, True),
    ('Wordwall Pro 1M', 4.00, True),
    ('YouTube Premium 12M Provider Account', 8.50, False),
    ('YouTube Premium 3 Months', 3.50, False),
    ('YouTube Premium 3M Abroad', 12.50, False),
    ('YouTube Premium 3M Vietnam', 10.00, False),
    ('YouTube Ready Account 3M', 2.20, False),
    ('Zoom Pro 14D', 2.05, True),
    ('Zoom Pro 1M Account', 3.30, True),
    ('Zoom Pro 28D', 4.10, True),
]


def sync_retail_catalog_details(db_conn, json_path: str = None) -> tuple:
    """Sync rich scraped descriptions, prices, and live stock from retail_catalog_details.json."""
    if json_path is None:
        base_dir = os.path.dirname(os.path.abspath(__file__))
        json_path = os.path.join(base_dir, "retail_catalog_details.json")

    if not os.path.exists(json_path):
        return 0, 0

    try:
        with open(json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return 0, 0

    from bot import detect_category
    cur = db_conn.cursor()

    rows = cur.execute("SELECT id, name, margin, fixed_price, supplier, auto_fulfill FROM products").fetchall()
    by_norm = {}
    for r in rows:
        pid = r[0] if isinstance(r, tuple) else r["id"]
        pname = r[1] if isinstance(r, tuple) else r["name"]
        pmargin = r[2] if isinstance(r, tuple) else r["margin"]
        pfixed = r[3] if isinstance(r, tuple) else r["fixed_price"]
        psup = r[4] if isinstance(r, tuple) else (r["supplier"] if "supplier" in r.keys() else "default")
        pauto = r[5] if isinstance(r, tuple) else (r["auto_fulfill"] if "auto_fulfill" in r.keys() else 0)
        norm = re.sub(r'[^a-z0-9]', '', str(pname).lower())
        if norm not in by_norm or (psup == 'default' or pauto):
            by_norm[norm] = (pid, pmargin if pmargin is not None else 45, pfixed)

    updated = 0
    inserted = 0
    for norm_k, item in data.items():
        name = item.get("item_name") or norm_k
        desc = item.get("description") or ""
        cost_usd = item.get("price_usd")
        stock = item.get("stock", 0)
        active = 1 if stock > 0 else 0
        cat = detect_category(name)

        if norm_k in by_norm:
            pid, margin_val, is_fixed = by_norm[norm_k]
            if cost_usd is not None and not is_fixed:
                cost_cents = int(round(cost_usd * 100))
                retail_cents = max(100, int(round(cost_cents * (1 + margin_val / 100.0))))
                cur.execute("""
                    UPDATE products
                    SET description = ?,
                        supplier_stock = ?,
                        active = ?,
                        supplier_price = ?,
                        price = ?,
                        category = CASE WHEN category = 'AI Tools' OR category IS NULL THEN ? ELSE category END
                    WHERE id = ?
                """, (desc, stock, active, cost_cents, retail_cents, cat, pid))
            else:
                cur.execute("""
                    UPDATE products
                    SET description = ?,
                        supplier_stock = ?,
                        active = ?,
                        category = CASE WHEN category = 'AI Tools' OR category IS NULL THEN ? ELSE category END
                    WHERE id = ?
                """, (desc, stock, active, cat, pid))
            updated += 1
        else:
            cost_cents = int(round((cost_usd or 0.0) * 100))
            retail_cents = max(100, int(round(cost_cents * 1.45)))
            cur.execute("""
                INSERT INTO products (
                    name, description, price, supplier_product_id, supplier_price,
                    auto_fulfill, active, category, supplier, supplier_stock,
                    delivery_type, margin, fixed_price
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'renqis_retail', ?, 'manual', 45, 0)
            """, (name, desc, retail_cents, f"renqis_retail_{norm_k}", cost_cents, active, cat, stock))
            inserted += 1
            by_norm[norm_k] = (cur.lastrowid, 45, 0)

    db_conn.commit()
    return updated, inserted


def seed_retail_catalog(db_conn):
    """Seed retail catalog products into database or update pricing/stock."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    json_path = os.path.join(base_dir, "retail_catalog_details.json")
    if os.path.exists(json_path):
        updated, inserted = sync_retail_catalog_details(db_conn, json_path=json_path)
        if updated > 0 or inserted > 0:
            return inserted

    cur = db_conn.cursor()
    
    # Lazy import to avoid circular dependency
    from bot import detect_category
    
    # Map normalized name to existing row id and margin/fixed_price state
    rows = cur.execute("SELECT id, name, margin, fixed_price, supplier, auto_fulfill FROM products").fetchall()
    existing_by_norm = {}
    for r in rows:
        pid = r[0] if isinstance(r, tuple) else r["id"]
        pname = r[1] if isinstance(r, tuple) else r["name"]
        pmargin = r[2] if isinstance(r, tuple) else r["margin"]
        pfixed = r[3] if isinstance(r, tuple) else r["fixed_price"]
        psup = r[4] if isinstance(r, tuple) else (r["supplier"] if "supplier" in r.keys() else "default")
        pauto = r[5] if isinstance(r, tuple) else (r["auto_fulfill"] if "auto_fulfill" in r.keys() else 0)
        norm = re.sub(r'[^a-z0-9]', '', str(pname).lower())
        if norm not in existing_by_norm or (psup == 'default' or pauto):
            existing_by_norm[norm] = (pid, pmargin, pfixed)
    
    inserted = 0
    updated = 0
    for idx, (name, cost_usd, is_in_stock) in enumerate(RETAIL_CATALOG_ITEMS, start=1):
        norm = re.sub(r'[^a-z0-9]', '', name.lower())
        cost_cents = int(round(cost_usd * 100))
        price_cents = max(100, int(round(cost_cents * 1.45)))
        active = 1 if is_in_stock else 0
        supplier_stock = 10 if is_in_stock else 0
        cat = detect_category(name)
        sp_id = f"renqis_retail_{idx}"
        
        if norm in existing_by_norm:
            pid, item_margin, is_fixed = existing_by_norm[norm]
            if not is_fixed:
                margin_val = item_margin if (item_margin is not None) else 45
                retail_cents = max(100, int(round(cost_cents * (1 + margin_val / 100))))
                cur.execute("""
                    UPDATE products 
                    SET price = ?,
                        supplier_price = ?,
                        supplier_stock = ?,
                        category = CASE WHEN category = 'AI Tools' OR category IS NULL THEN ? ELSE category END
                    WHERE id = ? AND fixed_price = 0
                """, (retail_cents, cost_cents, supplier_stock, cat, pid))
            else:
                cur.execute("""
                    UPDATE products 
                    SET supplier_price = ?,
                        supplier_stock = ?,
                        category = CASE WHEN category = 'AI Tools' OR category IS NULL THEN ? ELSE category END
                    WHERE id = ?
                """, (cost_cents, supplier_stock, cat, pid))
            updated += 1
            continue
            
        desc = (
            f"{name}\n\n"
            f"Delivery: Manual fulfillment by Store Admin within minutes.\n"
            f"Wholesale retail SKU from Renqis catalog."
        )
        
        cur.execute("""
            INSERT INTO products (
                name, description, price, supplier_product_id, supplier_price,
                auto_fulfill, active, category, supplier, supplier_stock,
                delivery_type, margin, fixed_price
            ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, 'renqis_retail', ?, 'manual', 45, 0)
        """, (name, desc, price_cents, sp_id, cost_cents, active, cat, supplier_stock))
        
        existing_by_norm[norm] = (cur.lastrowid, 45, 0)
        inserted += 1
        
    db_conn.commit()
    return inserted
