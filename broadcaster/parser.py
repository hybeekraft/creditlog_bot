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
        if re.search(r"(?:💵\s*)?Price\s*:\s*(?:[\$₦€£]\s*)?[\d.]+", text, re.IGNORECASE):
            result = self._process_single_item_format(text)
            if result:
                return result

        # Check list format (e.g. multi-product catalog: • Product A — 0.89 USD or - Product A - 0.89 USD)
        result = self._process_list_format(text)
        if result:
            return result

        return None

    def _process_single_item_format(self, text: str) -> Optional[Dict[str, Any]]:
        # Find price line: e.g. "💵 Price: 4.99 USD", "Price: $4.99", or "Price: 4.99"
        price_match = re.search(
            r"(?:💵\s*)?Price\s*:\s*(?:([\$₦€£])\s*)?([\d.]+)\s*([A-Za-z]+|[\$₦€£])?",
            text,
            re.IGNORECASE
        )
        if not price_match:
            return None

        sym_pre, price_val_str, sym_post = price_match.groups()
        base_price = float(price_val_str)
        curr_raw = (sym_post or sym_pre or "USD").strip().upper()
        curr_map = {"$": "USD", "₦": "NGN", "€": "EUR", "£": "GBP"}
        currency = curr_map.get(curr_raw, curr_raw if curr_raw.isalpha() else "USD")

        lines = [line.strip() for line in text.splitlines() if line.strip()]
        price_idx = -1
        for i, line in enumerate(lines):
            if re.search(r"Price\s*:\s*(?:[\$₦€£]\s*)?[\d.]+", line, re.IGNORECASE):
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

        # Recalculate bulk pricing section tiers using the active profit margin
        bulk_header_pattern = re.compile(r"(?:📦\s*)?(?:bulk\s*price|wholesale)", re.IGNORECASE)
        bulk_bullet_pattern = re.compile(
            r"^([•\-\*]\s*)(.+?)\s*([—\-–:])\s*(?:([\$₦€£])\s*)?([\d.]+)\s*([A-Za-z]+)?(.*)$",
            re.IGNORECASE
        )
        processed_lines = []
        in_bulk_section = False
        for line in transformed_text.splitlines():
            stripped = line.strip()
            if bulk_header_pattern.search(stripped):
                in_bulk_section = True
                processed_lines.append(line)
                continue

            m_tier = bulk_bullet_pattern.match(stripped)
            is_bulk_bullet = bool(
                m_tier and (
                    in_bulk_section or
                    re.search(r"\d+\+?\s*(?:pcs|acc|unit|item|x\b)|each|per\b", m_tier.group(2) + (m_tier.group(7) or ""), re.IGNORECASE)
                )
            )

            if is_bulk_bullet:
                prefix, tier_label, sep, curr_sym, price_str, curr_code, suffix = m_tier.groups()
                old_tier_price = float(price_str)
                new_tier_price = self.pricing_engine.calculate_margin_price(product_name, old_tier_price)

                curr_part = f"{curr_sym} " if curr_sym else ""
                price_part = f"{new_tier_price:.2f}"
                if curr_code:
                    price_part += f" {curr_code}"

                new_line = f"{prefix}{tier_label} {sep} {curr_part}{price_part}{suffix or ''}"
                processed_lines.append(new_line)
                continue

            if in_bulk_section and stripped and not m_tier:
                in_bulk_section = False

            processed_lines.append(line)

        transformed_text = re.sub(r"\n{3,}", "\n\n", "\n".join(processed_lines)).strip()

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
        # Regex for lines like: • Muse AI Account 31B Tokens — 0.89 USD or - Item - $0.89
        bullet_pattern = re.compile(
            r"([•\-\*]\s*)(.+?)\s*([—\-–:])\s*(?:([\$₦€£])\s*)?([\d.]+)\s*([A-Za-z]+|[\$₦€£])?",
            re.IGNORECASE
        )

        matches = bullet_pattern.findall(text)
        if not matches:
            return None

        transformed_text = text
        items: List[Dict[str, Any]] = []
        curr_map = {"$": "USD", "₦": "NGN", "€": "EUR", "£": "GBP"}

        for bullet_prefix, name, dash, sym_pre, price_str, sym_post in matches:
            base_price = float(price_str)
            clean_name = name.strip()
            curr_raw = (sym_post or sym_pre or "USD").strip().upper()
            curr = curr_map.get(curr_raw, curr_raw if curr_raw.isalpha() else "USD")

            new_price = self.pricing_engine.calculate_price(clean_name, base_price)

            new_line = f"{bullet_prefix}{clean_name} {dash} {new_price:.2f} {curr}"

            sym_pre_escaped = rf"(?:{re.escape(sym_pre)}\s*)?" if sym_pre else r"(?:[\$₦€£]\s*)?"
            sym_post_escaped = rf"(?:\s*{re.escape(sym_post)})?" if sym_post else r"(?:\s*[A-Za-z]+|\s*[\$₦€£])?"
            pattern_to_replace = re.compile(
                rf"{re.escape(bullet_prefix)}\s*{re.escape(name)}\s*{re.escape(dash)}\s*{sym_pre_escaped}{re.escape(price_str)}{sym_post_escaped}",
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
