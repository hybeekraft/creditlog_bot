"""
Reseller API Client for digital goods catalog.

Provider Base URL: https://api-geminipro.ignorelist.com/api/reseller/v1
Endpoints:
  - Products: GET /products
  - Product: GET /products/{productId}
  - Balance: GET /account/balance
  - Info: GET /account/info
  - Buy: POST /orders
  - Orders: GET /orders
  - Order: GET /orders/{orderCode}
"""
import os
import re
import logging
import aiohttp
from typing import Any, Dict, List, Optional

log = logging.getLogger("reseller")


class ResellerAPIError(Exception):
    """Exception raised when the Reseller API returns an error or unexpected status."""
    def __init__(self, message: str, status_code: Optional[int] = None, error_code: Optional[str] = None, raw: Any = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_code = error_code
        self.raw = raw


class ResellerClient:
    def __init__(
        self,
        base_url: str = "https://api-geminipro.ignorelist.com/api/reseller/v1",
        api_key: str = "",
        timeout: int = 30,
        session: Optional[aiohttp.ClientSession] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key.strip()
        self.timeout = aiohttp.ClientTimeout(total=timeout)
        self._session = session
        self._own_session = False

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=self.timeout)
            self._own_session = True
        return self._session

    async def close(self):
        if self._own_session and self._session and not self._session.closed:
            await self._session.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    def _headers(self) -> Dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        return headers

    async def _request(self, method: str, path: str, json_body: Optional[dict] = None) -> Any:
        if not self.api_key:
            raise ResellerAPIError("RESELLER_API_KEY is not configured.", error_code="missing_api_key")

        url = f"{self.base_url}/{path.lstrip('/')}"
        session = await self._get_session()

        try:
            async with session.request(method, url, headers=self._headers(), json=json_body) as resp:
                status = resp.status
                try:
                    data = await resp.json()
                except Exception:
                    text = await resp.text()
                    if status >= 400:
                        raise ResellerAPIError(f"API HTTP {status}: {text}", status_code=status, raw=text)
                    return {"raw_text": text}

                if status >= 400 or (isinstance(data, dict) and data.get("ok") is False):
                    err_code = data.get("error") if isinstance(data, dict) else None
                    msg = data.get("message") or data.get("error") if isinstance(data, dict) else f"HTTP {status}"
                    raise ResellerAPIError(
                        f"Supplier API error: {msg}",
                        status_code=status,
                        error_code=err_code,
                        raw=data,
                    )

                return data
        except aiohttp.ClientError as e:
            raise ResellerAPIError(f"Network error communicating with supplier: {e}") from e

    # -------------------------------------------------------------------------
    # API Methods
    # -------------------------------------------------------------------------

    async def get_products(self) -> List[Dict[str, Any]]:
        """
        Fetch available products from the reseller catalog.
        Returns a list of standardized product dicts:
          {
            "id": str,
            "name": str,
            "description": str,
            "price_cents": int,  # integer cents
            "price_usd": float,
            "stock": Optional[int],
            "raw": dict
          }
        """
        data = await self._request("GET", "/products")
        raw_list = []
        if isinstance(data, list):
            raw_list = data
        elif isinstance(data, dict):
            raw_list = data.get("products") or data.get("data") or data.get("items") or []

        normalized = []
        for item in raw_list:
            if not isinstance(item, dict):
                continue
            pid = str(item.get("productId") or item.get("id") or item.get("code") or item.get("_id") or "")
            if not pid:
                continue

            name = str(item.get("name") or item.get("title") or f"Product {pid}")
            desc = str(item.get("description") or item.get("desc") or "")

            raw_price = item.get("price") or item.get("cost") or item.get("unit_price") or 0
            try:
                price_val = float(raw_price)
                if isinstance(raw_price, float) or "." in str(raw_price):
                    price_cents = round(price_val * 100)
                else:
                    price_cents = round(price_val * 100) if price_val < 100 else int(price_val)
                price_usd = price_cents / 100.0
            except (ValueError, TypeError):
                price_cents = 0
                price_usd = 0.0

            stock_val = item.get("stock") or item.get("quantity") or item.get("inStock")
            try:
                stock_int = int(stock_val) if stock_val is not None else None
            except (ValueError, TypeError):
                stock_int = None

            normalized.append({
                "id": pid,
                "name": name,
                "description": desc,
                "price_cents": price_cents,
                "price_usd": price_usd,
                "stock": stock_int,
                "raw": item,
            })
        return normalized

    async def get_product(self, product_id: str) -> Dict[str, Any]:
        """Fetch a single product by productId."""
        data = await self._request("GET", f"/products/{product_id}")
        item = data.get("product") or data.get("data") if isinstance(data, dict) else data
        if not isinstance(item, dict):
            item = {"productId": product_id}

        pid = str(item.get("productId") or item.get("id") or product_id)
        name = str(item.get("name") or item.get("title") or f"Product {pid}")
        desc = str(item.get("description") or item.get("desc") or "")
        raw_price = item.get("price") or item.get("cost") or 0
        try:
            price_val = float(raw_price)
            if isinstance(raw_price, float) or "." in str(raw_price):
                price_cents = round(price_val * 100)
            else:
                price_cents = round(price_val * 100) if price_val < 100 else int(price_val)
            price_usd = price_cents / 100.0
        except (ValueError, TypeError):
            price_cents = 0
            price_usd = 0.0

        return {
            "id": pid,
            "name": name,
            "description": desc,
            "price_cents": price_cents,
            "price_usd": price_usd,
            "raw": item,
        }

    async def get_balance(self) -> Dict[str, Any]:
        """
        Fetch current account balance at supplier.
        Returns {'balance': float, 'currency': 'USD', 'raw': ...}
        """
        data = await self._request("GET", "/account/balance")
        bal = 0.0
        currency = "USD"
        if isinstance(data, dict):
            bal_raw = data.get("balance") or (data.get("data", {}) if isinstance(data.get("data"), dict) else {}).get("balance")
            if bal_raw is not None:
                try:
                    bal = float(bal_raw)
                except (ValueError, TypeError):
                    pass
            currency = data.get("currency") or "USD"
        return {"balance": bal, "currency": currency, "raw": data}

    async def get_info(self) -> Dict[str, Any]:
        """Fetch account information."""
        data = await self._request("GET", "/account/info")
        return data if isinstance(data, dict) else {"data": data}

    async def create_order(self, product_id: str, quantity: int = 1) -> Dict[str, Any]:
        """
        Send POST to Buy URL (/orders) with productId and quantity.
        Returns:
          {
            "order_code": str,
            "value": str,      # deliver this to buyer!
            "quantity": int,
            "raw": dict
          }
        """
        body = {
            "productId": product_id,
            "quantity": quantity,
        }
        data = await self._request("POST", "/orders", json_body=body)

        # Provider response structure:
        # Typical: {"ok": true, "order": {"orderCode": "...", "value": "...", ...}}
        # or {"value": "...", "orderCode": "..."}
        order_obj = data
        if isinstance(data, dict):
            if isinstance(data.get("order"), dict):
                order_obj = data["order"]
            elif isinstance(data.get("data"), dict):
                order_obj = data["data"]

        value = None
        order_code = None

        if isinstance(order_obj, dict):
            value = (
                order_obj.get("value")
                or order_obj.get("code")
                or order_obj.get("key")
                or order_obj.get("payload")
                or order_obj.get("delivered")
            )
            order_code = (
                order_obj.get("orderCode")
                or order_obj.get("id")
                or order_obj.get("code")
                or order_obj.get("order_id")
            )

        # Fallback to top-level if not inside order object
        if value is None and isinstance(data, dict):
            value = data.get("value") or data.get("code") or data.get("key")
        if order_code is None and isinstance(data, dict):
            order_code = data.get("orderCode") or data.get("order_id")

        if isinstance(value, list):
            value_str = "\n".join(str(v) for v in value)
        elif value is not None:
            value_str = str(value)
        else:
            value_str = ""

        return {
            "order_code": str(order_code or ""),
            "value": value_str,
            "quantity": quantity,
            "raw": data,
        }

    async def get_orders(self) -> List[Dict[str, Any]]:
        """Fetch list of orders from supplier."""
        data = await self._request("GET", "/orders")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("orders") or data.get("data") or []
        return []

    async def get_order(self, order_code: str) -> Dict[str, Any]:
        """Fetch a specific order by orderCode."""
        data = await self._request("GET", f"/orders/{order_code}")
        return data if isinstance(data, dict) else {"data": data}


class SupplierRegistry:
    """
    Registry for managing multiple reseller/supplier API clients.
    Supports:
      - Default / primary supplier (backward-compatible with RESELLER_BASE_URL & RESELLER_API_KEY)
      - Multiple named suppliers configured via environment variables
        (SUPPLIER_<NAME>_KEY and SUPPLIER_<NAME>_URL, e.g. SUPPLIER_AI_KEY, SUPPLIER_STREAMING_KEY)
        or added programmatically.
      - Catalog fetching, balance checking, and order creation routed to specific suppliers.
    """

    def __init__(self):
        self._suppliers: Dict[str, ResellerClient] = {}
        self._default_name: str = "default"

    def register(self, name: str, client: ResellerClient, is_default: bool = False):
        name = name.strip().lower()
        self._suppliers[name] = client
        if is_default or not self._default_name or self._default_name not in self._suppliers:
            self._default_name = name

    def get(self, name: Optional[str] = None) -> Optional[ResellerClient]:
        if not name:
            name = self._default_name
        name = name.strip().lower()
        return self._suppliers.get(name) or self._suppliers.get(self._default_name)

    def list_suppliers(self) -> Dict[str, ResellerClient]:
        return dict(self._suppliers)

    def default_name(self) -> str:
        return self._default_name

    @classmethod
    def from_env(cls) -> "SupplierRegistry":
        """
        Auto-discover suppliers from environment variables:
          1. Legacy/default:
             RESELLER_BASE_URL and RESELLER_API_KEY -> 'default'
          2. Named suppliers:
             SUPPLIER_<NAME>_KEY & optional SUPPLIER_<NAME>_URL
             e.g.:
               SUPPLIER_STREAMING_KEY=...
               SUPPLIER_STREAMING_URL=https://...
               SUPPLIER_GAMES_KEY=...
               SUPPLIER_GAMES_URL=https://...
        """
        registry = cls()
        def_url = os.environ.get("RESELLER_BASE_URL") or "https://api-geminipro.ignorelist.com/api/reseller/v1"
        def_key = os.environ.get("RESELLER_API_KEY", "").strip()
        if def_key:
            registry.register("default", ResellerClient(base_url=def_url, api_key=def_key), is_default=True)

        for env_k, env_v in os.environ.items():
            m = re.match(r"^SUPPLIER_([A-Z0-9_]+)_KEY$", env_k)
            if m and env_v.strip():
                name = m.group(1).lower()
                url = os.environ.get(f"SUPPLIER_{m.group(1)}_URL") or def_url
                is_def = (name == "default") or (not registry._suppliers)
                registry.register(name, ResellerClient(base_url=url, api_key=env_v.strip()), is_default=is_def)

        return registry

    async def get_all_balances(self) -> Dict[str, Dict[str, Any]]:
        results = {}
        for name, client in self._suppliers.items():
            try:
                results[name] = await client.get_balance()
            except Exception as e:
                results[name] = {"error": str(e), "balance": 0.0, "currency": "USD"}
        return results

    async def close_all(self):
        for client in self._suppliers.values():
            await client.close()

