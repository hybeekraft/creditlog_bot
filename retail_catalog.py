"""Supplier retail catalog items missing from automated reseller API feed."""

import re

# (name, cost_usd, is_in_stock)
RETAIL_CATALOG_ITEMS = [
    ('AntiGravity Argon/Hydrogen 3M FAM', 4.00, True),
    ('Fallout 76 MS Store', 2.43, True),
    ('Gamma AI Plus 1 Month', 10.00, True),
    ('Gamma Pro 12M Coupon Code', 46.00, False),
    ('Gamma Pro 1M', 14.00, True),
    ('HeyGen Creator 1M 28D', 25.00, False),
    ('HeyGen Creator 1M 48H', 19.23, True),
    ('Higgsfield Starter 1M', 10.00, False),
    ('KREA Max', 36.50, True),
    ('Kiro Pro Edu Official Owner 1Y', 73.00, False),
    ('Lovable Pro 12 Months', 45.00, True),
    ('Loveable Pro 1M Redeem Link', 1.20, False),
    ('Manus Pro 1 Year Coupon', 58.00, False),
    ('Muse AI 31 Billion Tokens', 0.99, False),
    ('Muse AI 31B Gmail Stable', 1.70, True),
    ('Muse AI Account 1B Tokens', 0.30, False),
    ('Muse AI Account 31B Tokens', 0.89, True),
    ('Scribd Premium 1M', 2.10, True),
    ('Veo Ultra X20 0 Credits 1M', 17.50, False),
    ('Veo Ultra X20 25K Credits Share 1M', 23.00, True),
    ('iCloud 2TB 3M', 11.50, False),
    ('iCloud 400GB Your Mail 12M', 24.00, True),
    ('Adobe All Apps 7D', 3.67, False),
    ('Adobe CC Pro 1M', 20.27, True),
    ('Autodesk 1 App 3Y Upgrade', 5.00, False),
    ('Autodesk All Apps 1 Year', 1.00, True),
    ('Autodesk Full Apps 3Y Upgrade', 8.00, False),
    ('ChatGPT Business Team 1M', 17.00, False),
    ('ChatGPT Business Team 1M Invite', 18.50, True),
    ('ChatGPT Plus 4M', 36.17, False),
    ('ChatGPT Plus Your Mail 1M', 20.04, False),
    ('ChatGPT Plus Your Mail 4M', 43.00, False),
    ('100$ Claude API 30D FableAI', 2.50, False),
    ('500$ Claude API 30D FableAI', 7.70, False),
    ('Claude API 100M Tokens 1 Day', 6.00, True),
    ('Claude API 100M Tokens 3 Days', 7.50, True),
    ('Claude API 10M Tokens 1D', 3.00, True),
    ('Claude API 500M Tokens 1 Day', 20.00, False),
    ('Claude API 500M Tokens 6D', 23.00, False),
    ('Claude API 50M Tokens 1 Day', 5.00, True),
    ('Claude API 50M Tokens 3 Days', 6.00, False),
    ('API Codex Astra $100 1M', 9.08, True),
    ('API Codex Astra $1000 1M', 60.48, True),
    ('API Codex Astra $200 1M', 13.00, True),
    ('API Codex Astra $300 1M', 18.15, True),
    ('API Codex Astra $500 1M', 30.26, True),
    ('Codex API 100M Tokens 1 Day', 5.00, True),
    ('Codex API 10M Tokens 1 Day', 3.60, True),
    ('Codex API 500M Tokens 6 Days', 29.80, True),
    ('Codex API 500M Tokens 6D', 20.00, False),
    ('Codex API 50M Tokens 1 Day', 4.20, True),
    ('Cursor Pro API 2600 Credits 1 Month', 11.50, True),
    ('Cursor Pro API 400 Credits/day 1 Month', 15.50, True),
    ('Cursor Pro API 6500 Credits 1 Month', 15.50, True),
    ('DeepSeek API $10 1 Month', 2.20, True),
    ('DeepSeek API $100 1 Month', 6.10, True),
    ('DeepSeek API $30 1 Month', 2.90, True),
    ('DeepSeek API $50 1 Month', 4.10, True),
    ('Canva Education 12M', 2.50, False),
    ('Canva Pro Add to Team 12 Months', 4.50, True),
    ('Freepik Magnific API Panel 30D', 5.00, False),
    ('Discord Decor 66K Profile Effect', 6.00, False),
    ('Discord Decor 79K Profile Effect', 6.50, False),
    ('Discord Decor 92K Profile Effect', 7.50, False),
    ('Discord Gifting Badge x1', 5.50, False),
    ('Discord Nitro 3M Token', 5.50, True),
    ('Discord Nitro Trial 3M', 7.50, False),
    ('Duolingo Super 2 Months Link', 2.50, True),
    ('Super Duolingo Your Mail 12 Months', 9.00, True),
    ('ElevenLabs Redeem 1M Credits', 12.54, True),
    ('ElevenLabs Redeem 3M Credits', 34.50, True),
    ('PC Game Pass 1M', 8.38, True),
    ('Gemini API $10 1M', 2.50, True),
    ('Gemini API $30 1M', 3.50, True),
    ('Gemini API $50 1M', 5.00, True),
    ('Gemini Pro 12 Months FAM', 7.54, True),
    ('Gemini Pro 3 Months FAM', 3.50, True),
    ('Gemini Pro 6 Months FAM', 5.16, True),
    ('CDK Grok Super 1M', 22.50, False),
    ('CDK Heavy Grok 1M', 34.99, False),
    ('CDK Super Grok Old 1M', 26.50, False),
    ('CDK Super Grok Old 3M', 46.00, False),
    ('Grok + Cursor Ultra 1M', 68.50, True),
    ('Grok API 4.5 HIGH/XHIGH 100M Tokens 7 Days', 5.00, True),
    ('Grok API 4.5 HIGH/XHIGH 15M Tokens 1 Day', 2.50, True),
    ('Grok Plus 12M', 40.00, True),
    ('Grok Super 1M', 33.50, True),
    ('Grok Super 1M (5 Days Warranty)', 38.50, False),
    ('API Kimi 100M Tokens 7D', 6.00, True),
    ('API Kimi 15M Tokens 1D', 3.00, True),
    ('API Kimi 300M Tokens 30D', 10.00, True),
    ('Kimi GLM DeepSeek API 100M Tokens 4D', 5.00, True),
    ('Kimi GLM DeepSeek API 500M Tokens 7D', 14.00, True),
    ('Kimi GLM DeepSeek API 50M Tokens 2D', 2.70, True),
    ('Spotify Individual 12M', 22.00, False),
    ('Spotify Individual 3M Activation Link', 2.50, True),
    ('Spotify Premium 6 Months', 9.80, False),
    ('Notion Business 6M', 17.50, True),
    ('Notion Business Admin 1M', 7.70, False),
    ('O365 Ready Account Random 6-12M', 4.00, True),
    ('Windows 10 / 11 Pro Permanent Key', 5.00, True),
    ('Zoom Pro 1M Account', 3.30, True),
    ('API QWEN Unlimited Tokens 7D', 5.00, True),
    ('McAfee AntiVirus 1 Device 1Y', 7.27, True),
    ('Netflix 4K Private Slot 1M', 4.00, True),
    ('Netflix Premium 4K Slot 1M', 5.22, True),
    ('YouTube Premium 12M Provider Account', 8.50, False),
    ('YouTube Premium 3M Abroad', 12.50, False),
    ('YouTube Premium 3M Vietnam', 10.00, False),
    ('ExpressVPN 3 Days', 1.10, True),
    ('Kaspersky Premium + VPN 1Y', 9.60, True),
    ('NordVPN 10 Devices 6M', 16.16, False),
    ('Proton Unlimited 1M', 5.50, False),
    ('Kling Pro 3K3 Credits 1M', 26.00, False),
]


def seed_retail_catalog(db_conn):
    """Seed retail catalog products into database if not already present."""
    cur = db_conn.cursor()
    
    # Get existing normalized names
    existing = {
        re.sub(r'[^a-z0-9]', '', r[0].lower())
        for r in cur.execute("SELECT name FROM products").fetchall()
    }
    
    # Lazy import to avoid circular dependency
    from bot import detect_category
    
    inserted = 0
    for idx, (name, cost_usd, is_in_stock) in enumerate(RETAIL_CATALOG_ITEMS, start=1):
        norm = re.sub(r'[^a-z0-9]', '', name.lower())
        if norm in existing:
            continue
            
        cost_cents = int(round(cost_usd * 100))
        price_cents = max(100, int(round(cost_cents * 1.45)))
        active = 1 if is_in_stock else 0
        supplier_stock = 10 if is_in_stock else 0
        cat = detect_category(name)
        sp_id = f"renqis_retail_{idx}"
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
        
        existing.add(norm)
        inserted += 1
        
    db_conn.commit()
    return inserted
