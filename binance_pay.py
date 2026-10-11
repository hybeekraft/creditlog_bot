"""
Binance Pay OpenAPI v2 Integration for Telegram Reseller Store.
Supports dynamic checkout orders, instant in-app payment links (universalUrl / deeplink),
automated order status queries, and payment verification.
"""

import hashlib
import hmac
import json
import logging
import os
import random
import string
import time
from typing import Any, Dict, Optional, Tuple
import aiohttp

log = logging.getLogger("binance_pay")

DEFAULT_BASE_URL = "https://bpay.binanceapi.com"


class BinancePayClient:
    """Official Binance Pay OpenAPI v2 client."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        base_url: str = DEFAULT_BASE_URL,
    ):
        self.api_key = (api_key or os.environ.get("BINANCE_PAY_API_KEY", "")).strip()
        self.secret_key = (secret_key or os.environ.get("BINANCE_PAY_SECRET_KEY", "")).strip()
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")

    def is_configured(self) -> bool:
        """Returns True if valid merchant API credentials are configured."""
        if not self.api_key or not self.secret_key:
            return False
        if self.api_key.startswith("your_") or self.secret_key.startswith("your_"):
            return False
        return True

    def generate_nonce(self, length: int = 32) -> str:
        """Generates a random alphanumeric nonce."""
        chars = string.ascii_letters + string.digits
        return "".join(random.choices(chars, k=length))

    def build_headers(
        self,
        body_str: str,
        timestamp: Optional[str] = None,
        nonce: Optional[str] = None,
    ) -> Dict[str, str]:
        """
        Builds mandatory Binance Pay authentication headers.
        Signature = HMAC-SHA512 of (timestamp + '\n' + nonce + '\n' + body + '\n')
        """
        ts = timestamp or str(int(time.time() * 1000))
        n = nonce or self.generate_nonce()
        payload_to_sign = f"{ts}\n{n}\n{body_str}\n"

        signature = hmac.new(
            self.secret_key.encode("utf-8"),
            payload_to_sign.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest().upper()

        return {
            "Content-Type": "application/json",
            "BinancePay-Timestamp": ts,
            "BinancePay-Nonce": n,
            "BinancePay-Certificate-SN": self.api_key,
            "BinancePay-Signature": signature,
        }

    async def create_order(
        self,
        merchant_trade_no: str,
        amount_usd: float,
        goods_name: str,
        goods_id: str = "1",
        goods_detail: Optional[str] = None,
        goods_type: str = "02",
        goods_category: str = "6000",
        currency: str = "USDT",
        return_url: Optional[str] = None,
        cancel_url: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> Tuple[bool, Dict[str, Any], str]:
        """
        Creates a Binance Pay V2 checkout order.
        Returns: (success: bool, order_data: dict, error_message: str)
        order_data includes prepayId, universalUrl, checkoutUrl, qrContent, etc.
        """
        if not self.is_configured():
            return False, {}, "Binance Pay merchant credentials are not configured."

        payload = {
            "env": {"terminalType": "APP"},
            "merchantTradeNo": str(merchant_trade_no),
            "orderAmount": round(float(amount_usd), 2),
            "currency": currency,
            "goods": {
                "goodsType": goods_type,
                "goodsCategory": goods_category,
                "referenceGoodsId": str(goods_id),
                "goodsName": goods_name[:32],
                "goodsDetail": (goods_detail or goods_name)[:256],
            },
        }
        if return_url:
            payload["returnUrl"] = return_url
        if cancel_url:
            payload["cancelUrl"] = cancel_url

        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self.build_headers(body_str)
        url = f"{self.base_url}/binancepay/openapi/v2/order"

        close_session = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
            close_session = True

        try:
            async with session.post(url, data=body_str, headers=headers) as resp:
                status_code = resp.status
                data = await resp.json()

                if status_code == 200 and data.get("status") == "SUCCESS" and data.get("code") == "000000":
                    order_data = data.get("data") or {}
                    return True, order_data, ""
                else:
                    err = data.get("errorMessage") or f"Binance error code: {data.get('code')}"
                    log.warning("Binance Pay create_order failed: %s (status %s)", data, status_code)
                    return False, data, err
        except Exception as e:
            log.exception("Binance Pay create_order exception: %s", e)
            return False, {}, str(e)
        finally:
            if close_session and not session.closed:
                await session.close()

    async def query_order(
        self,
        merchant_trade_no: Optional[str] = None,
        prepay_id: Optional[str] = None,
        session: Optional[aiohttp.ClientSession] = None,
    ) -> Tuple[bool, Dict[str, Any], str]:
        """
        Queries status of an order via Binance Pay OpenAPI v2.
        Returns: (success: bool, order_data: dict, error_message: str)
        order_data['status'] is typically 'PAID', 'PENDING', 'INITIAL', 'EXPIRED', or 'CANCELED'.
        """
        if not self.is_configured():
            return False, {}, "Binance Pay merchant credentials are not configured."

        payload: Dict[str, Any] = {}
        if prepay_id:
            payload["prepayId"] = str(prepay_id)
        elif merchant_trade_no:
            payload["merchantTradeNo"] = str(merchant_trade_no)
        else:
            return False, {}, "Either merchantTradeNo or prepayId must be provided."

        body_str = json.dumps(payload, separators=(",", ":"))
        headers = self.build_headers(body_str)
        url = f"{self.base_url}/binancepay/openapi/v2/order/query"

        close_session = False
        if session is None or session.closed:
            session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
            close_session = True

        try:
            async with session.post(url, data=body_str, headers=headers) as resp:
                status_code = resp.status
                data = await resp.json()

                if status_code == 200 and data.get("status") == "SUCCESS" and data.get("code") == "000000":
                    order_data = data.get("data") or {}
                    return True, order_data, ""
                else:
                    err = data.get("errorMessage") or f"Binance error code: {data.get('code')}"
                    log.warning("Binance Pay query_order failed: %s (status %s)", data, status_code)
                    return False, data, err
        except Exception as e:
            log.exception("Binance Pay query_order exception: %s", e)
            return False, {}, str(e)
        finally:
            if close_session and not session.closed:
                await session.close()

    def verify_webhook_signature(
        self,
        timestamp: str,
        nonce: str,
        body_str: str,
        signature: str,
    ) -> bool:
        """Verifies incoming webhook payload signature."""
        if not self.is_configured():
            return False
        payload_to_sign = f"{timestamp}\n{nonce}\n{body_str}\n"
        expected = hmac.new(
            self.secret_key.encode("utf-8"),
            payload_to_sign.encode("utf-8"),
            hashlib.sha512,
        ).hexdigest().upper()
        return hmac.compare_digest(expected, signature.upper().strip())


# Default global instance initialized from environment
client = BinancePayClient()
