"""
Utility script to test message parsing, price calculations, and store.db deep linking
without needing a live Telegram connection.
"""
from pathlib import Path
try:
    from .parser import MessageParser
    from .pricing import PricingEngine
    from .broadcaster import find_store_product_id, MY_BOT_USERNAME, DB_PATH
except ImportError:
    from parser import MessageParser
    from pricing import PricingEngine
    from broadcaster import find_store_product_id, MY_BOT_USERNAME, DB_PATH

BASE_DIR = Path(__file__).resolve().parent

SAMPLE_1 = """🔥 Restocked!

ChatGPT Plus 1M Apple Pay

💵 Price: 4.99 USD

⚡ Instant delivery after payment
⌛ Limited stock available"""

SAMPLE_2 = """🔥 Restocked!

Airtable Team 12 Months

💵 Price: 10.00 USD

⚡ Instant delivery after payment
⌛ Limited stock available"""

SAMPLE_3 = """🔥 Restocked!

CapCut Pro 7D 1200 Credits

💵 Price: 0.90 USD

⚡ Instant delivery after payment
⌛ Limited stock available"""

SAMPLE_4 = """🔥 Restocked

These products are available again:

• Muse AI Account 31B Tokens — 0.89 USD"""

def test_samples():
    pricing_engine = PricingEngine(BASE_DIR / "pricing_rules.json", db_path=DB_PATH)
    parser = MessageParser(pricing_engine)

    samples = [
        ("Single Item (ChatGPT Plus - Fixed Markup)", SAMPLE_1),
        ("Single Item (Airtable Team - Matches store.db #1)", SAMPLE_2),
        ("Single Item (CapCut Pro - Fixed Markup)", SAMPLE_3),
        ("List Format (Muse AI)", SAMPLE_4)
    ]

    print("=" * 60)
    print("CREDITLOG BOT STOCK BROADCASTER - SIMULATION TEST")
    print("=" * 60)

    for title, text in samples:
        print(f"\n[TEST CASE]: {title}")
        print("--- Original Supplier Message ---")
        print(text)
        print("--- Transformed Broadcast Output ---")
        result = parser.process_message(text)
        if result:
            print(result["transformed_text"])
            
            # Check deep link
            bot_url = f"https://t.me/{MY_BOT_USERNAME}"
            if result["type"] == "single" and result["items"]:
                matched_id = find_store_product_id(result["items"][0]["name"])
                if matched_id:
                    bot_url = f"https://t.me/{MY_BOT_USERNAME}?start=p_{matched_id}"
                    print(f"-> [DB MATCH]: Found product #{matched_id} in store.db")

            print(f"[Button]: {result['button_text']} -> {bot_url}")
            print("Extracted Items:")
            for itm in result["items"]:
                print(f"  • {itm['name']}: {itm['old_price']} {itm['currency']} -> {itm['new_price']} {itm['currency']}")
        else:
            print("NO MATCH (ignored)")
        print("-" * 60)

if __name__ == "__main__":
    test_samples()
