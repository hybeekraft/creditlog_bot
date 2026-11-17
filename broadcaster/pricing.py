import json
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Any, Optional, Union

DEFAULT_RULES_FILE = Path(__file__).parent / "pricing_rules.json"

class PricingEngine:
    """Calculates updated prices using Decimal arithmetic and configurable rules."""

    def __init__(self, config_path: Union[Path, str] = DEFAULT_RULES_FILE):
        self.config_path = Path(config_path)
        self.rules = self.load_rules()

    def load_rules(self) -> Dict[str, Any]:
        default = {
            "default_rule": {"type": "percentage", "value": 30.0},
            "custom_rules": []
        }
        if not self.config_path.exists():
            return default
        try:
            with open(self.config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return default

    def calculate_price(self, product_name: str, base_price: float) -> float:
        """Finds matching rule (keyword or default) and applies price adjustment."""
        rule = self.rules.get("default_rule", {"type": "percentage", "value": 30.0})

        product_lower = product_name.lower()
        for custom in self.rules.get("custom_rules", []):
            if custom.get("keyword", "").lower() in product_lower:
                rule = custom
                break

        rule_type = rule.get("type", "percentage")
        value_dec = Decimal(str(rule.get("value", 0.0)))
        base_dec = Decimal(str(base_price))

        if rule_type == "percentage":
            multiplier = Decimal("1") + (value_dec / Decimal("100"))
            final_dec = base_dec * multiplier
        elif rule_type == "fixed":
            final_dec = base_dec + value_dec
        else:
            final_dec = base_dec

        # Round to 2 decimal places using standard commercial rounding (half up)
        rounded = final_dec.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return float(rounded)
