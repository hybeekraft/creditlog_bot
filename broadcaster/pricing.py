import json
import os
import re
import sqlite3
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Any, Optional, Union, List

DEFAULT_RULES_FILE = Path(__file__).parent / "pricing_rules.json"

class PricingEngine:
    """Calculates updated prices using store.db profit margins, locked SKU prices, and Decimal math."""

    def __init__(self, config_path: Union[Path, str] = DEFAULT_RULES_FILE, db_path: Optional[Union[Path, str]] = None):
        self.config_path = Path(config_path)
        self.rules = self.load_rules()
        self.db_path = Path(db_path) if db_path else None

    def load_rules(self) -> Dict[str, Any]:
        default = {
            "default_rule": {"type": "percentage", "value": 45.0},
            "custom_rules": []
        }
        if not self.config_path.exists():
            return default
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def get_store_margin(self) -> float:
        """Reads active store profit margin percentage from store.db settings or environment."""
        if self.db_path and self.db_path.exists():
            conn = None
            try:
                conn = sqlite3.connect(self.db_path)
                cur = conn.cursor()
                cur.execute("SELECT value FROM settings WHERE key='profit_margin'")
                row = cur.fetchone()
                if row and row[0]:
                    return float(str(row[0]).strip().rstrip("%"))
            except Exception:
                pass
            finally:
                if conn:
                    conn.close()

        env_val = os.getenv("PROFIT_MARGIN") or os.getenv("RESELLER_MARKUP_PERCENT")
        if env_val:
            try:
                return float(str(env_val).strip().rstrip("%"))
            except Exception:
                pass

        default_rule = self.rules.get("default_rule", {})
        if default_rule.get("type") == "percentage":
            return float(default_rule.get("value", 45.0))

        return 45.0

    def find_store_product(self, product_name: str) -> Optional[dict]:
        """Looks up product in store.db to check for SKU-specific fixed price or custom margin."""
        if not self.db_path or not self.db_path.exists():
            return None

        conn = None
        try:
            def clean_words(s: str) -> List[str]:
                cleaned = re.sub(r'[^a-zA-Z0-9\s]', ' ', s).lower()
                return [w for w in cleaned.split() if w]

            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("SELECT id, name, active, price, fixed_price, margin, supplier_price FROM products")
            rows = cur.fetchall()

            q_words = clean_words(product_name)
            q_set = set(q_words)
            q_lower = ' '.join(q_words)

            GENERIC = {'api', 'account', 'redeem', 'credits', 'pack', 'plan', 'pro', 'month', 'months', 'year', 'years', '1m', '1y', '3m', '6m', '12m', '1', '2', '3', '6', '12', 'days', '7d', '28'}

            # 1. Exact match pass (active first, then inactive)
            for active_target in (1, 0):
                for row in rows:
                    pid, name, active, price, fixed_price, margin, supplier_price = row
                    if active != active_target:
                        continue
                    n_words = clean_words(name)
                    if ' '.join(n_words) == q_lower:
                        return {
                            "id": pid, "name": name, "price": price,
                            "fixed_price": fixed_price, "margin": margin,
                            "supplier_price": supplier_price
                        }

            # 2. Substring pass in normalized text (active first, then inactive)
            for active_target in (1, 0):
                for row in rows:
                    pid, name, active, price, fixed_price, margin, supplier_price = row
                    if active != active_target:
                        continue
                    n_words = clean_words(name)
                    n_lower = ' '.join(n_words)
                    if not n_lower:
                        continue
                    brand_token = next((w for w in n_words if w not in GENERIC), n_words[0] if n_words else "")
                    if brand_token and brand_token in q_set and (n_lower in q_lower or q_lower in n_lower):
                        return {
                            "id": pid, "name": name, "price": price,
                            "fixed_price": fixed_price, "margin": margin,
                            "supplier_price": supplier_price
                        }

            # 2. Key brand token match
            best_match = None
            best_score = 0.0
            for active_target in (1, 0):
                for row in rows:
                    pid, name, active, price, fixed_price, margin, supplier_price = row
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
                        best_match = {
                            "id": pid, "name": name, "price": price,
                            "fixed_price": fixed_price, "margin": margin,
                            "supplier_price": supplier_price
                        }
                if best_match:
                    return best_match

        except Exception:
            return None
        finally:
            if conn:
                conn.close()

        return None

    def calculate_price(self, product_name: str, base_price: float) -> float:
        """
        Calculates selling price for product:
        - If SKU has a fixed price set via /setprice, uses that exact locked price.
        - If SKU has a custom margin set via /setmargin, applies that margin.
        - Otherwise, applies the store's active profit margin percentage (from /s_sync).
        - If no DB is configured, falls back to pricing_rules.json.
        """
        base_dec = Decimal(str(base_price))

        p = self.find_store_product(product_name)
        if p:
            # 1. Product price locked via /setprice
            if p.get("fixed_price") == 1 and p.get("price"):
                retail_cents = p["price"]
                price_dec = Decimal(str(retail_cents)) / Decimal("100")
                return float(price_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

            # 2. Product custom margin set via /setmargin
            if p.get("margin") is not None:
                margin_val = Decimal(str(p["margin"]))
                multiplier = Decimal("1") + (margin_val / Decimal("100"))
                final_dec = base_dec * multiplier
                return float(final_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        # 3. Active store profit margin (from store.db settings or sync)
        if self.db_path and self.db_path.exists():
            store_margin = self.get_store_margin()
            multiplier = Decimal("1") + (Decimal(str(store_margin)) / Decimal("100"))
            final_dec = base_dec * multiplier
            return float(final_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

        # Fallback to pricing_rules.json if no database is configured (e.g. standalone test)
        rule = self.rules.get("default_rule", {"type": "percentage", "value": 30.0})
        product_lower = product_name.lower()
        for custom in self.rules.get("custom_rules", []):
            if custom.get("keyword", "").lower() in product_lower:
                rule = custom
                break

        rule_type = rule.get("type", "percentage")
        value_dec = Decimal(str(rule.get("value", 30.0)))

        if rule_type == "percentage":
            multiplier = Decimal("1") + (value_dec / Decimal("100"))
            final_dec = base_dec * multiplier
        elif rule_type == "fixed":
            final_dec = base_dec + value_dec
        else:
            final_dec = base_dec

        return float(final_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))
