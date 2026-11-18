import re
from typing import Optional, Dict, Any, List

try:
    from .pricing import PricingEngine
except ImportError:
    from pricing import PricingEngine

class MessageParser:
    """Parses supplier broadcast messages, recalculates prices, and reconstructs the message."""

    def __init__(self, pricing_engine: Optional[PricingEngine] = None):
        self.pricing_engine = pricing_engine or PricingEngine()

    def process_message(self, text: str) -> Optional[Dict[str, Any]]:
        """
        Processes an incoming supplier message.
        Returns a dict with transformed text and metadata, or None if not a recognized product alert.
        """
        if not text:
            return None

        # Check single-item format first (e.g. Price: 0.30 USD, even if it has bulk price bullets)
        if re.search(r"(?:💵\s*)?Price\s*:\s*[\d.]+", text, re.IGNORECASE):
            result = self._process_single_item_format(text)
            if result:
                return result

        # Check list format (e.g. multi-product catalog: • Product A — 0.89 USD)
        if "•" in text or "—" in text:
            result = self._process_list_format(text)
            if result:
                return result

        return None

    def _process_single_item_format(self, text: str) -> Optional[Dict[str, Any]]:
        # Find price line: e.g. "💵 Price: 4.99 USD" or "Price: 4.99 USD"
        price_match = re.search(r"(?:💵\s*)?Price\s*:\s*([\d.]+)\s*([A-Za-z]+)", text, re.IGNORECASE)
        if not price_match:
            return None

        base_price = float(price_match.group(1))
        currency = price_match.group(2).upper()

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        price_idx = -1
        for i, line in enumerate(lines):
            if re.search(r"Price\s*:\s*[\d.]+", line, re.IGNORECASE):
                price_idx = i
                break

        if price_idx <= 0:
            return None

        HEADER_KEYWORDS = ("restock", "new product", "new item", "alert", "update", "product added", "available")
        preceding = lines[:price_idx]
        if len(preceding) == 1:
            product_name = preceding[0]
        else:
            first_lower = preceding[0].lower()
            if any(k in first_lower for k in HEADER_KEYWORDS):
                product_name = preceding[1]
            else:
                product_name = preceding[-1]

        if not product_name:
            product_name = "Product"

        new_price = self.pricing_engine.calculate_price(product_name, base_price)

        # Replace price in original text while preserving format and emojis
        old_price_str = price_match.group(0)
        has_dollar_icon = "💵" in old_price_str
        icon_prefix = "💵 " if has_dollar_icon else ""
        new_price_str = f"{icon_prefix}Price: {new_price:.2f} {currency}"
        transformed_text = text.replace(old_price_str, new_price_str, 1)

        # Also recalculate any bulk pricing tiers in the message
        bulk_pattern = re.compile(
            r"^([•\-\*]\s*)(.+?)\s*([—\-–:])\s*([\d.]+)\s*([A-Za-z]+)(.*)$",
            re.IGNORECASE
        )
        transformed_lines = []
        for line in transformed_text.splitlines():
            bulk_match = bulk_pattern.match(line.strip())
            if bulk_match:
                bullet_prefix, tier_desc, sep, old_bulk_price_str, b_curr, suffix = bulk_match.groups()
                try:
                    old_bulk_price = float(old_bulk_price_str)
                    new_bulk_price = self.pricing_engine.calculate_price(product_name, old_bulk_price)
                    line = f"{bullet_prefix}{tier_desc} {sep} {new_bulk_price:.2f} {b_curr.upper()}{suffix}"
                except ValueError:
                    pass
            transformed_lines.append(line)
        transformed_text = "\n".join(transformed_lines)

        return {
            "type": "single",
            "transformed_text": transformed_text,
            "button_text": "🛍️ Open item",
            "items": [
                {
                    "name": product_name,
                    "old_price": base_price,
                    "new_price": new_price,
                    "currency": currency
                }
            ]
        }

    def _process_list_format(self, text: str) -> Optional[Dict[str, Any]]:
        # Regex for lines like: • Muse AI Account 31B Tokens — 0.89 USD
        bullet_pattern = re.compile(
            r"([•\-\*]\s*)(.+?)\s*([—\-–])\s*([\d.]+)\s*([A-Za-z]+)",
            re.IGNORECASE
        )

        matches = bullet_pattern.findall(text)
        if not matches:
            return None

        transformed_text = text
        items: List[Dict[str, Any]] = []

        for bullet_prefix, name, dash, price_str, currency in matches:
            base_price = float(price_str)
            clean_name = name.strip()
            curr = currency.strip().upper()

            new_price = self.pricing_engine.calculate_price(clean_name, base_price)

            old_line_match = f"{bullet_prefix}{name}{dash}{price_str} {currency}".strip()
            # Construct updated line
            new_line = f"{bullet_prefix}{clean_name} {dash} {new_price:.2f} {curr}"

            # Replace line occurrence in text
            pattern_to_replace = re.compile(
                rf"{re.escape(bullet_prefix)}\s*{re.escape(name)}\s*{re.escape(dash)}\s*{re.escape(price_str)}\s*{re.escape(currency)}",
                re.IGNORECASE
            )
            transformed_text = pattern_to_replace.sub(new_line, transformed_text, count=1)

            items.append({
                "name": clean_name,
                "old_price": base_price,
                "new_price": new_price,
                "currency": curr
            })

        return {
            "type": "list",
            "transformed_text": transformed_text,
            "button_text": "🛍️ Shop",
            "items": items
        }
