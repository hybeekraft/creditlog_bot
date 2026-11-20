"""Authenticated HTTP adapter over the Telegram bot's existing services."""

import asyncio
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import smtplib
import sqlite3
import ssl
import time
from email.message import EmailMessage
from datetime import datetime, timezone
from urllib.parse import quote

from aiohttp import web

TELEGRAM_BOT_KEY = web.AppKey("telegram_bot", object)
log = logging.getLogger("creditlog.web_api")


def _json(status, **body):
    return web.json_response(body, status=status, headers={"Cache-Control": "no-store"})


def _setting(service, key, default=""):
    return service["get_setting"](key, default)


def _log(service, actor, action, details=""):
    service["db"].execute(
        "INSERT INTO web_audit_logs(actor, action, details, created_at) VALUES(?,?,?,?)",
        (actor, action, details[:1000], datetime.now(timezone.utc).isoformat()),
    )


async def _send_customer_email(address, subject, body):
    host = os.environ.get("SMTP_HOST", "").strip()
    if not host or not address:
        return False
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ.get("SMTP_FROM", os.environ.get("SMTP_USERNAME", "support@creditlog.com"))
    message["To"] = address
    message.set_content(body)
    port = int(os.environ.get("SMTP_PORT", "587"))
    username = os.environ.get("SMTP_USERNAME", "")
    password = os.environ.get("SMTP_PASSWORD", "")

    def send():
        if port == 465:
            with smtplib.SMTP_SSL(host, port, timeout=10, context=ssl.create_default_context()) as smtp:
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
        else:
            with smtplib.SMTP(host, port, timeout=10) as smtp:
                smtp.ehlo()
                if os.environ.get("SMTP_STARTTLS", "1").lower() not in ("0", "false", "off"):
                    smtp.starttls(context=ssl.create_default_context())
                    smtp.ehlo()
                if username:
                    smtp.login(username, password)
                smtp.send_message(message)
    try:
        await asyncio.to_thread(send)
        return True
    except Exception as exc:
        log.warning("Customer email delivery failed: %s", type(exc).__name__)
        return False


def _password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 240_000)
    return f"{salt.hex()}:{digest.hex()}"


def _password_matches(password, encoded):
    try:
        salt, expected = encoded.split(":", 1)
        actual = _password_hash(password, bytes.fromhex(salt)).split(":", 1)[1]
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _session_token(secret, username, role):
    payload = {"sub": username, "role": role, "exp": int(time.time()) + 3600}
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).rstrip(b"=")
    signature = hmac.new(secret.encode(), encoded, hashlib.sha256).digest()
    return encoded.decode() + "." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


def _session_claims(secret, token):
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(secret.encode(), encoded.encode(), hashlib.sha256).digest()
        actual = base64.urlsafe_b64decode(signature + "=" * (-len(signature) % 4))
        if not hmac.compare_digest(actual, expected):
            return None
        payload = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        claims = json.loads(payload)
        return claims if claims.get("exp", 0) > time.time() else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _bearer(req):
    value = req.headers.get("Authorization", "")
    return value[7:].strip() if value.lower().startswith("bearer ") else ""


def _claims(req):
    secret = os.environ.get("CREDITLOG_BACKEND_SECRET", "")
    if not secret:
        return None
    return _session_claims(secret, _bearer(req))


def _has_role(claims, *roles):
    role = (claims or {}).get("role")
    allowed = {
        "admin": {"admin", "manager", "support"},
        "manager": {"manager", "support"},
        "support": {"support"},
    }
    return bool(role and set(roles) & allowed.get(role, set()))


def _require_admin(req, *roles):
    claims = _claims(req)
    if not claims:
        return None, _json(401, success=False, message="Administrator session expired.")
    if not _has_role(claims, *roles):
        return None, _json(403, success=False, message="This account cannot perform that action.")
    return claims, None


def _product(service, row):
    data = dict(row)
    stock_count = service["stock_count"](data["id"])
    is_supplier = bool(data.get("auto_fulfill") and data.get("supplier_product_id"))
    stock = data.get("supplier_stock") if is_supplier else stock_count
    available = 999999 if is_supplier and stock is None else max(0, int(stock or 0))
    price = int(data.get("price") or 0)
    cost = int(data.get("supplier_price") or 0)
    return {
        "id": str(data["id"]), "botProductId": data["id"],
        "name": data["name"], "title": data["name"],
        "description": data.get("description") or "",
        "brand": data.get("web_brand") or (data["name"].split()[0] if data["name"] else ""),
        "logoUrl": data.get("web_logo_url") or "",
        "importantRequirements": data.get("web_requirements") or "",
        "howItIsDelivered": data.get("web_delivery_instructions") or "",
        "duration": data.get("web_duration") or "",
        "category": data.get("category") or "AI Tools",
        "botCategory": data.get("category") or "AI Tools",
        "usdPrice": round(price / 100, 2), "price": round(price / 100, 2),
        "supplierPrice": round(cost / 100, 2),
        "resellerProductId": data.get("supplier_product_id"),
        "supplier": data.get("supplier") or "default",
        "deliveryFormat": "Automatic supplier delivery" if is_supplier else "Local stock delivery",
        "totalStock": available, "availableStock": available,
        "inStock": bool(data.get("active") and (stock is None or available > 0)),
        "enabled": bool(data.get("active")), "active": bool(data.get("active")),
        "autoFulfill": bool(data.get("auto_fulfill")),
        "deliveryType": data.get("delivery_type") or "auto",
        "variants": json.loads(data.get("web_options_json") or "[]"),
        "margin": data.get("margin"), "fixedPrice": bool(data.get("fixed_price")),
    }


def _selected_product_price(row, duration):
    options = json.loads(row["web_options_json"] or "[]")
    if duration and options:
        selected = next((option for option in options if str(option.get("duration", "")).strip() == duration), None)
        if not selected:
            raise ValueError("Select an available product option.")
        price = round(float(selected.get("usdPrice", int(row["price"]) / 100)) * 100)
        return price, duration
    if duration and row["web_duration"] and duration != row["web_duration"]:
        raise ValueError("Select an available product option.")
    return int(row["price"]), duration or row["web_duration"] or ""


def _order(service, row, include_private=False):
    data = dict(row)
    order_id = str(data["id"])
    result = {
        "id": f"#CL-{order_id}", "botOrderId": data["id"],
        "product": data.get("product_name") or "Digital subscription",
        "productId": str(data.get("product_id") or ""),
        "quantity": int(data.get("quantity") or 1),
        "price": f"${int(data.get('price') or 0) / 100:.2f}",
        "totalUsd": round(int(data.get("price") or 0) / 100, 2),
        "gateway": data.get("payment_method") or "",
        "status": {"completed": "Delivered", "paid": "Paid", "manual_pending": "Processing", "pending": "Pending", "refunded": "Refunded", "rejected": "Rejected", "cancelled": "Cancelled", "failed": "Failed"}.get(data.get("status"), str(data.get("status") or "Pending").title()),
        "date": data.get("created") or "",
        "totalNgn": round(int(data.get("price") or 0) / 100 * service["get_usd_ngn_rate"]()),
        "reference": data.get("invoice_id") or data.get("txid") or "",
        "supplierOrderCode": data.get("supplier_order_id"),
        "fulfillmentStatus": data.get("status"),
    }
    if include_private:
        result.update({
            "customerName": data.get("customer_name") or "Customer",
            "customerEmail": data.get("customer_email") or "",
            "customerPhone": data.get("customer_phone") or "",
            "deliveryContact": data.get("delivery_contact") or "",
            "plan": data.get("web_plan") or "",
            "productSpecificInfo": data.get("customer_notes") or "",
            "deliveryChannel": data.get("delivery_channel") or "Email",
            "deliveredValue": data.get("payload") or "",
            "credentials": {"value": data.get("payload") or ""},
            "refunded": bool(data.get("refunded")),
            "expectedAmount": data.get("expected_amount") or "",
        })
    return result


def _lookup_order(service, identifier):
    cleaned = str(identifier or "").strip().removeprefix("#CL-").removeprefix("#")
    if not cleaned.isdigit():
        return None
    return service["db"].execute("SELECT * FROM orders WHERE id=?", (int(cleaned),)).fetchone()


def _migrate_schema(service):
    db = service["db"]
    existing = {row[1] for row in db.execute("PRAGMA table_info(orders)").fetchall()}
    additions = {
        "customer_name": "TEXT DEFAULT ''", "customer_email": "TEXT DEFAULT ''",
        "customer_phone": "TEXT DEFAULT ''", "delivery_channel": "TEXT DEFAULT 'Email'",
        "web_order_token_hash": "TEXT DEFAULT NULL", "web_currency": "TEXT DEFAULT 'USD'",
        "customer_notes": "TEXT DEFAULT ''", "web_plan": "TEXT DEFAULT ''",
        "delivery_contact": "TEXT DEFAULT ''",
    }
    for name, definition in additions.items():
        if name not in existing:
            db.execute(f"ALTER TABLE orders ADD COLUMN {name} {definition}")
    product_columns = {row[1] for row in db.execute("PRAGMA table_info(products)").fetchall()}
    product_additions = {
        "web_brand": "TEXT DEFAULT ''", "web_logo_url": "TEXT DEFAULT ''",
        "web_requirements": "TEXT DEFAULT ''", "web_delivery_instructions": "TEXT DEFAULT ''",
        "web_options_json": "TEXT DEFAULT '[]'", "web_duration": "TEXT DEFAULT ''",
    }
    for name, definition in product_additions.items():
        if name not in product_columns:
            db.execute(f"ALTER TABLE products ADD COLUMN {name} {definition}")
    db.execute("CREATE INDEX IF NOT EXISTS idx_web_orders_email ON orders(customer_email)")
    db.execute("""CREATE TABLE IF NOT EXISTS web_admins(
        username TEXT PRIMARY KEY, password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'admin', active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS web_audit_logs(
        id INTEGER PRIMARY KEY AUTOINCREMENT, actor TEXT NOT NULL,
        action TEXT NOT NULL, details TEXT DEFAULT '', created_at TEXT NOT NULL)""")
    username = os.environ.get("WEB_ADMIN_USERNAME", "").strip()
    password = os.environ.get("WEB_ADMIN_PASSWORD", "")
    if username and password:
        db.execute(
            "INSERT OR IGNORE INTO web_admins(username,password_hash,role,active,created_at) VALUES(?,?,'admin',1,?)",
            (username, _password_hash(password), datetime.now(timezone.utc).isoformat()),
        )


def _supplier_configs(service):
    try:
        value = json.loads(_setting(service, "web_supplier_configs", "[]"))
        return value if isinstance(value, list) else []
    except (TypeError, json.JSONDecodeError):
        return []


def _apply_supplier_configs(service, configs):
    registry = service["supplier_registry"]
    clients = registry._suppliers
    default_name = registry.default_name()
    for item in configs:
        name = item["id"]
        client = clients.get(name)
        if client is None:
            client = service["ResellerClient"](base_url=item["baseUrl"], api_key=item["apiKey"])
            registry.register(name, client, is_default=bool(item.get("isDefault")))
        else:
            client.base_url = item["baseUrl"].rstrip("/")
            client.api_key = item["apiKey"].strip()
        if item.get("isDefault"):
            registry._default_name = name
    if not any(item.get("isDefault") for item in configs) and default_name in clients:
        registry._default_name = default_name
    elif not any(item.get("isDefault") for item in configs) and clients:
        registry._default_name = next(iter(clients))


def _supplier_vendor_list(service):
    registry = service["supplier_registry"]
    return [{"id": name, "name": name, "baseUrl": client.base_url,
             "apiKeyMasked": "••••••••" if client.api_key else "",
             "isConfigured": bool(client.api_key), "isDefault": name == registry.default_name(),
             "mode": "live", "currency": "USD"}
            for name, client in registry.list_suppliers().items()]


def register_web_routes(app, service):
    _migrate_schema(service)
    stored_suppliers = _supplier_configs(service)
    if stored_suppliers:
        _apply_supplier_configs(service, stored_suppliers)
    for setting, variable in (("cryptobot_token", "CRYPTOBOT_TOKEN"), ("binance_pay_id", "BINANCE_PAY_ID"), ("crypto_usdt_trc20", "CRYPTO_USDT_TRC20"), ("crypto_usdt_bep20", "CRYPTO_USDT_BEP20"), ("support_url", "SUPPORT_URL"), ("channel_url", "CHANNEL_URL")):
        saved = _setting(service, setting, "")
        if saved:
            service[variable] = saved
    try:
        service["LOW_STOCK"] = int(_setting(service, "low_stock", str(service["LOW_STOCK"])))
    except (ValueError, TypeError):
        pass

    @web.middleware
    async def backend_auth(req, handler):
        if not req.path.startswith("/api/"):
            return await handler(req)
        expected = os.environ.get("CREDITLOG_BACKEND_SECRET", "")
        supplied = req.headers.get("X-CreditLog-Backend-Secret", "")
        if not expected or not hmac.compare_digest(expected, supplied):
            return _json(401, success=False, message="Backend access denied.")
        return await handler(req)

    app.middlewares.append(backend_auth)

    async def products(req):
        db = service["db"]
        sql = "SELECT * FROM products WHERE active=1 ORDER BY category, name"
        rows = db.execute(sql).fetchall()
        items = [_product(service, row) for row in rows]
        category = req.query.get("category", "all").lower()
        search = req.query.get("search", "").strip().lower()
        if category not in ("", "all"):
            items = [p for p in items if p["category"].lower() == category]
        if search:
            items = [p for p in items if search in p["name"].lower() or search in p["description"].lower()]
        return _json(200, success=True, count=len(items), exchangeRate={"USD_TO_NGN": service["get_usd_ngn_rate"]()}, data=items)

    async def product_detail(req):
        row = service["db"].execute("SELECT * FROM products WHERE id=? AND active=1", (req.match_info["product_id"],)).fetchone()
        if not row:
            return _json(404, success=False, message="Product not found.")
        return _json(200, success=True, exchangeRate={"USD_TO_NGN": service["get_usd_ngn_rate"]()}, data=_product(service, row))

    async def quote(req):
        try:
            body = await req.json()
            quantity = int(body.get("quantity", body.get("qty", 1)))
            product_id = int(body.get("productId"))
        except (ValueError, TypeError, json.JSONDecodeError):
            return _json(400, success=False, message="A valid product and quantity are required.")
        if not 1 <= quantity <= 100:
            return _json(422, success=False, message="Quantity must be between 1 and 100.")
        row = service["db"].execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
        if not row:
            return _json(404, success=False, message="Product not found or unavailable.")
        available, count = service["check_product_stock_available"](row, quantity)
        duration = str(body.get("duration", "")).strip()[:100]
        try:
            unit_cents, duration = _selected_product_price(row, duration)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return _json(422, success=False, message=str(exc) or "Product option is invalid.")
        total_cents = unit_cents * quantity
        rate = service["get_usd_ngn_rate"]()
        usd_total = round(total_cents / 100, 2)
        ngn_total = round(usd_total * rate)
        currency = body.get("currency", "NGN").upper()
        return _json(200, success=True, productId=str(product_id), productName=row["name"], quantity=quantity,
                     availableStock=count, inStock=bool(available), duration=duration, unitPriceUsd=round(unit_cents / 100, 2),
                     totalUsd=usd_total, totalNgn=ngn_total, currency=currency,
                     formattedPrice=f"${usd_total:.2f}" if currency == "USD" else f"₦{ngn_total:,}")

    async def create_order(req):
        try:
            body = await req.json()
            product_id = int(body.get("productId"))
            quantity = int(body.get("quantity", 1))
        except (ValueError, TypeError, json.JSONDecodeError):
            return _json(400, success=False, message="A valid product and quantity are required.")
        name = str(body.get("customerName", body.get("name", ""))).strip()[:160]
        email = str(body.get("customerEmail", body.get("email", ""))).strip().lower()[:254]
        phone = str(body.get("customerPhone", body.get("phone", ""))).strip()[:40]
        if not name or "@" not in email or len(email) > 254:
            return _json(422, success=False, message="A name and valid email address are required.")
        if not 1 <= quantity <= 100:
            return _json(422, success=False, message="Quantity must be between 1 and 100.")
        row = service["db"].execute("SELECT * FROM products WHERE id=? AND active=1", (product_id,)).fetchone()
        if not row:
            return _json(404, success=False, message="Product not found or unavailable.")
        available, count = service["check_product_stock_available"](row, quantity)
        if not available:
            return _json(409, success=False, message=f"Only {count} units are available.")
        method = str(body.get("gateway", "bachs")).strip().lower()
        allowed = {"bachs", "bank", "cryptobot", "binance", "trc20", "bep20"}
        if method not in allowed:
            return _json(422, success=False, message="Unsupported payment method.")
        duration = str(body.get("duration", "")).strip()[:100]
        try:
            unit_cents, duration = _selected_product_price(row, duration)
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            return _json(422, success=False, message=str(exc) or "Product option is invalid.")
        total = unit_cents * quantity
        token = secrets.token_urlsafe(32)
        cur = service["db"].execute(
            "INSERT INTO orders(user_id,product_id,product_name,price,quantity,payment_method,status,customer_name,customer_email,customer_phone,delivery_channel,web_order_token_hash,web_currency,customer_notes,web_plan,delivery_contact) VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (0, product_id, row["name"], total, quantity, method, name, email, phone,
             str(body.get("deliveryChannel", "Email"))[:40], hashlib.sha256(token.encode()).hexdigest(),
             str(body.get("currency", "NGN")).upper(), str(body.get("productSpecificInfo", ""))[:2000] + ("\n" + str(body.get("notes", ""))[:2000] if body.get("notes") else ""), duration, str(body.get("deliveryContact", ""))[:254]),
        )
        order_id = int(cur.lastrowid)
        order = service["db"].execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        checkout_url = None
        payment = {}
        if method == "bachs":
            ok, payment = await service["initialize_bachs_transaction"](
                order_id, total, row["name"], email, None, quantity,
                success_url_override=(os.environ.get("WEB_SUCCESS_URL", "https://creditlog.com/success.html") + ("&" if "?" in os.environ.get("WEB_SUCCESS_URL", "https://creditlog.com/success.html") else "?") + "orderId=" + quote(str(order_id))),
                cancel_url_override=os.environ.get("WEB_CANCEL_URL", ""),
            )
            if not ok:
                service["db"].execute("DELETE FROM orders WHERE id=?", (order_id,))
                return _json(503, success=False, message="Bachs checkout is unavailable.")
            checkout_url = payment["checkout_url"]
            service["db"].execute("UPDATE orders SET invoice_id=?,txid=?,expected_amount=? WHERE id=?",
                                  (payment["checkout_id"], payment["checkout_id"], f"{payment['amount']} {payment['currency']}", order_id))
        elif method == "cryptobot":
            try:
                invoice = await service["create_cryptobot_invoice"](order_id, row["name"], round(total * 1.03))
            except Exception:
                service["db"].execute("DELETE FROM orders WHERE id=?", (order_id,))
                return _json(503, success=False, message="CryptoBot checkout is unavailable.")
            service["db"].execute("UPDATE orders SET invoice_id=? WHERE id=?", (str(invoice["invoice_id"]), order_id))
            checkout_url = invoice.get("bot_invoice_url") or invoice.get("pay_url")
        elif method == "bank":
            accounts = service["get_bank_accounts"]()
            if not accounts:
                service["db"].execute("DELETE FROM orders WHERE id=?", (order_id,))
                return _json(503, success=False, message="Bank transfer is unavailable.")
            rate = service["get_usd_ngn_rate"]()
            payment = {"accounts": accounts, "amountNgn": round(total / 100 * rate), "reference": f"CL-{order_id}"}
            service["db"].execute("UPDATE orders SET expected_amount=?,invoice_id=? WHERE id=?",
                                  (f"₦{payment['amountNgn']:,} NGN", f"BANK-{order_id}", order_id))
        elif method == "binance":
            payment = {"method": method, "payId": service["BINANCE_PAY_ID"],
                       "amountUsd": round(total / 100, 2),
                       "instructions": "Pay this amount to the Binance Pay ID, then submit the transaction ID for administrator verification."}
            service["db"].execute("UPDATE orders SET expected_amount=? WHERE id=?", (f"{total / 100:.2f} USDT", order_id))
        else:
            suffix = (order_id % 90) + 10
            exact_amount = f"{total / 100:.2f}{suffix:02d}"
            address = service["CRYPTO_USDT_TRC20"] if method == "trc20" else service["CRYPTO_USDT_BEP20"]
            payment = {"method": method, "address": address, "exactAmount": exact_amount,
                       "instructions": "Send the exact USDT amount on the selected network, then submit the transaction hash for verification."}
            service["db"].execute("UPDATE orders SET expected_amount=? WHERE id=?", (f"{exact_amount} USDT", order_id))
        order = service["db"].execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
        result = _order(service, order, include_private=True)
        result["paymentMethod"] = method
        result["payment"] = {key: payment.get(key) for key in ("checkout_id", "checkout_url", "status", "amount", "currency", "accounts", "amountNgn", "reference", "payId", "address", "exactAmount", "instructions") if key in payment}
        result["checkoutUrl"] = checkout_url
        result["accessToken"] = token
        payment_lines = []
        if checkout_url:
            payment_lines.append(f"Complete payment here: {checkout_url}")
        if payment.get("accounts"):
            payment_lines.append(f"Bank accounts: {json.dumps(payment['accounts'])}")
            payment_lines.append(f"Amount: ₦{payment['amountNgn']:,}; reference: {payment['reference']}")
        if payment.get("address"):
            payment_lines.append(f"Send exactly {payment['exactAmount']} USDT to {payment['address']} on {method.upper()}.")
        if payment.get("payId"):
            payment_lines.append(f"Send {payment['amountUsd']:.2f} USDT to Binance Pay ID {payment['payId']}.")
        if payment.get("instructions"):
            payment_lines.append(payment["instructions"])
        store_url = os.environ.get("WEB_STORE_URL", "https://creditlog.com").rstrip("/")
        track_url = f"{store_url}/success.html?orderId={order_id}#access={token}"
        await _send_customer_email(email, f"CreditLog order {result['id']} created", f"Hi {name},\n\nYour order for {row['name']} has been created.\nOrder: {result['id']}\nAmount: ${total / 100:.2f}\nTrack your order: {track_url}\n\n" + "\n".join(payment_lines))
        return _json(201, success=True, message="Order created.", order=result)

    async def track_order(req):
        identifier = req.query.get("id", "").strip()
        if identifier:
            row = _lookup_order(service, identifier)
            token = req.headers.get("X-Order-Access", "")
            if not row or not token or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row["web_order_token_hash"] or ""):
                return _json(404, success=False, message="Order not found.")
            order = _order(service, row, include_private=True)
            return _json(200, success=True, count=1, data=[order])
        return _json(400, success=False, message="Enter an order ID.")

    async def submit_payment_reference(req):
        try:
            body = await req.json()
        except json.JSONDecodeError:
            return _json(400, success=False, message="Invalid request body.")
        row = _lookup_order(service, req.match_info["order_id"])
        if not row:
            return _json(404, success=False, message="Order not found.")
        token = str(body.get("accessToken", ""))
        if not token or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row["web_order_token_hash"] or ""):
            return _json(403, success=False, message="Order access could not be verified.")
        reference = str(body.get("reference", "")).strip()[:200]
        if not reference or row["status"] not in ("pending", "paid", "manual_pending"):
            return _json(422, success=False, message="A transaction reference is required for a pending order.")
        service["db"].execute("UPDATE orders SET txid=? WHERE id=?", (reference, row["id"]))
        _log(service, "Customer", "Submit payment reference", f"Order {row['id']}")
        return _json(200, success=True, message="Reference submitted for payment review.")

    async def payment_verify(req):
        try:
            body = await req.json()
        except json.JSONDecodeError:
            return _json(400, success=False, message="Invalid request body.")
        row = _lookup_order(service, body.get("orderId"))
        if not row:
            return _json(404, success=False, message="Order not found.")
        token = str(body.get("accessToken", ""))
        if not token or not hmac.compare_digest(hashlib.sha256(token.encode()).hexdigest(), row["web_order_token_hash"] or ""):
            return _json(403, success=False, message="Order access could not be verified.")
        if row["status"] in ("paid", "manual_pending", "completed"):
            return _json(200, success=True, message="Payment already processed.", order=_order(service, row, include_private=True))
        method = (row["payment_method"] or "").lower()
        verification = {}
        if method == "bachs" and row["invoice_id"]:
            paid, verification = await service["verify_bachs_transaction"](row["invoice_id"])
        elif method == "cryptobot" and row["invoice_id"]:
            paid = await service["check_cryptobot_invoice"](int(row["invoice_id"]))
        elif method in ("trc20", "bep20"):
            tx_hash = str(body.get("transactionHash", "")).strip()
            if not tx_hash:
                return _json(422, success=False, message="Submit the blockchain transaction hash.")
            wallet = service["CRYPTO_USDT_TRC20"] if method == "trc20" else service["CRYPTO_USDT_BEP20"]
            verify = service["onchain"].verify_trc20_usdt if method == "trc20" else service["onchain"].verify_bep20_usdt
            try:
                expected_amount = float(str(row["expected_amount"]).split()[0])
            except (ValueError, TypeError, IndexError):
                expected_amount = int(row["price"] or 0) / 100
            duplicate = service["db"].execute("SELECT id FROM orders WHERE txid=? AND id!=? AND status IN ('paid','completed','manual_pending','refunded')", (tx_hash, row["id"])).fetchone()
            if duplicate:
                return _json(409, success=False, message="This transaction has already been used for another order.")
            paid, message, amount = await verify(tx_hash, wallet, expected_amount)
            verification = {"amount": amount, "currency": "USD", "message": message}
            if paid:
                service["db"].execute("UPDATE orders SET txid=? WHERE id=?", (tx_hash, row["id"]))
        elif method in ("bank", "binance"):
            return _json(202, success=True, message="Payment reference is awaiting administrator verification.", order=_order(service, row))
        else:
            return _json(422, success=False, message="This payment method is not available for verification.")
        if not paid:
            return _json(402, success=False, message="Payment is not confirmed yet.")
        expected = int(row["price"] or 0) / 100
        amount = verification.get("amount")
        currency = (verification.get("currency") or service["get_bachs_currency"]()).upper()
        if amount is not None:
            try:
                received = float(amount)
            except (ValueError, TypeError):
                return _json(409, success=False, message="Payment amount could not be verified.")
            if currency == "NGN":
                expected *= service["get_usd_ngn_rate"]()
            tolerance = 1 if currency == "NGN" else 0.01
            if received + tolerance < expected:
                return _json(409, success=False, message="The verified payment amount is below the order total.")
        if method == "bachs" and verification.get("checkout_id") != row["invoice_id"]:
            return _json(409, success=False, message="The verified checkout does not match this order.")
        service["db"].execute("UPDATE orders SET status='paid' WHERE id=? AND status='pending'", (row["id"],))
        bot = req.app.get(TELEGRAM_BOT_KEY)
        if not bot:
            return _json(503, success=False, message="Order fulfillment is temporarily unavailable.")
        payload, error = await service["fulfill_order"](bot, int(row["id"]))
        updated = service["db"].execute("SELECT * FROM orders WHERE id=?", (row["id"],)).fetchone()
        if not payload and updated["status"] not in ("manual_pending", "completed"):
            return _json(502, success=False, message="Payment is confirmed; fulfillment needs administrator attention.", order=_order(service, updated))
        result = _order(service, updated, include_private=True)
        result["deliveryError"] = error
        await _send_customer_email(row["customer_email"], f"CreditLog order {_order(service, updated)['id']} update", f"Your payment is confirmed. Order status: {updated['status']}." + (f"\n\nDelivery:\n{updated['payload']}" if updated["payload"] else " Your order is being prepared."))
        return _json(200, success=True, message="Payment verified.", order=result)

    async def bachs_webhook(req):
        secret = os.environ.get("BACHS_WEBHOOK_SECRET", "").strip()
        if not secret:
            return _json(503, success=False, message="Bachs webhook verification is not configured.")
        raw = await req.read()
        signature = req.headers.get("X-Bachs-Signature") or req.headers.get("X-Bachs-Signature-V2", "")
        timestamp = req.headers.get("X-Bachs-Timestamp", "")
        candidates = [hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()]
        if timestamp:
            candidates.append(hmac.new(secret.encode(), timestamp.encode() + b"." + raw, hashlib.sha256).hexdigest())
        if not any(hmac.compare_digest(signature, candidate) for candidate in candidates):
            return _json(401, success=False, message="Invalid payment callback signature.")
        try:
            body = json.loads(raw or b"{}")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return _json(400, success=False, message="Invalid callback body.")
        event = str(body.get("event") or body.get("type") or "").lower()
        if event and event not in ("collection.succeeded", "checkout.completed", "charge.succeeded", "charge.success", "payment.succeeded"):
            return _json(200, success=True, message="Callback ignored.")
        data = body.get("data") if isinstance(body.get("data"), dict) else body
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        row = _lookup_order(service, metadata.get("order_id") or data.get("orderId"))
        if not row:
            reference = str(data.get("reference") or data.get("id") or data.get("sessionId") or "")
            row = service["db"].execute("SELECT * FROM orders WHERE invoice_id=? OR txid=? LIMIT 1", (reference, reference)).fetchone()
        if not row:
            return _json(404, success=False, message="Order not found.")
        if row["status"] in ("paid", "manual_pending", "completed"):
            return _json(200, success=True, message="Order is already processed.")
        if row["payment_method"] != "bachs" or not row["invoice_id"]:
            return _json(409, success=False, message="This order does not have a Bachs checkout.")
        paid, verification = await service["verify_bachs_transaction"](row["invoice_id"])
        if not paid or verification.get("checkout_id") != row["invoice_id"]:
            return _json(402, success=False, message="Bachs has not confirmed this checkout.")
        amount = verification.get("amount")
        currency = str(verification.get("currency") or service["get_bachs_currency"]()).upper()
        try:
            received = float(amount)
        except (ValueError, TypeError):
            return _json(409, success=False, message="Verified payment amount is missing.")
        expected = int(row["price"] or 0) / 100
        if currency == "NGN":
            expected *= service["get_usd_ngn_rate"]()
        if received < expected:
            return _json(409, success=False, message="Verified payment is below the order total.")
        service["db"].execute("UPDATE orders SET status='paid' WHERE id=? AND status='pending'", (row["id"],))
        bot = req.app.get(TELEGRAM_BOT_KEY)
        if not bot:
            return _json(503, success=False, message="Order fulfillment is temporarily unavailable.")
        await service["fulfill_order"](bot, int(row["id"]))
        updated = service["db"].execute("SELECT * FROM orders WHERE id=?", (row["id"],)).fetchone()
        _log(service, "Bachs webhook", "Confirm payment", f"Order {row['id']}")
        await _send_customer_email(row["customer_email"], f"CreditLog order #CL-{row['id']} update", f"Your payment is confirmed. Current order status: {updated['status']}.")
        return _json(200, success=True, message="Payment verified.", order=_order(service, updated, True))

    async def login(req):
        try:
            body = await req.json()
        except json.JSONDecodeError:
            return _json(400, success=False, message="Invalid request body.")
        username = str(body.get("username", body.get("email", ""))).strip().lower()
        password = str(body.get("password", ""))
        row = service["db"].execute("SELECT * FROM web_admins WHERE lower(username)=? AND active=1", (username,)).fetchone()
        if not row or not _password_matches(password, row["password_hash"]):
            _log(service, "unknown", "Admin login failed", "Invalid credentials")
            return _json(401, success=False, message="Invalid username or password.")
        secret = os.environ.get("CREDITLOG_BACKEND_SECRET", "")
        if not secret:
            return _json(503, success=False, message="Web admin authentication is not configured.")
        _log(service, username, "Admin login", "Web dashboard session created")
        return _json(200, success=True, token=_session_token(secret, row["username"], row["role"]),
                     user={"name": row["username"], "email": row["username"], "role": row["role"].title()})

    async def admin_inventory(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        rows = service["db"].execute("SELECT * FROM products ORDER BY category,name").fetchall()
        items = [_product(service, row) for row in rows]
        low_stock = sum(1 for p in items if p["enabled"] and p["totalStock"] <= service["LOW_STOCK"])
        return _json(200, success=True, count=len(items), products=items,
                     summary={"totalProducts": len(items), "totalStockUnits": sum(p["totalStock"] for p in items),
                              "totalAvailableUnits": sum(p["availableStock"] for p in items),
                              "outOfStockCount": sum(not p["inStock"] for p in items), "lowStockCount": low_stock})

    async def admin_orders(req):
        claims, error = _require_admin(req, "admin", "manager", "support")
        if error:
            return error
        limit = min(200, max(1, int(req.query.get("limit", "50"))))
        offset = max(0, int(req.query.get("offset", "0")))
        rows = service["db"].execute("SELECT * FROM orders ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        orders = [_order(service, row, include_private=True) for row in rows]
        return _json(200, success=True, total=service["db"].execute("SELECT COUNT(*) FROM orders").fetchone()[0],
                     limit=limit, offset=offset, orders=orders)

    async def admin_logs(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        rows = service["db"].execute("SELECT * FROM web_audit_logs ORDER BY id DESC LIMIT 500").fetchall()
        return _json(200, success=True, count=len(rows), logs=[dict(row) for row in rows])

    async def admin_product_create(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        try:
            body = await req.json()
            name = str(body.get("name", "")).strip()[:180]
            price = round(float(body.get("usdPrice", body.get("price", 0))) * 100)
            quantity = max(0, min(100000, int(body.get("totalStock", 0))))
            stock_items = body.get("stockItems", [])
            if not isinstance(stock_items, list) or len(stock_items) > 1000 or any(not isinstance(item, str) or not item.strip() for item in stock_items):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            return _json(422, success=False, message="Product name, price, and stock must be valid.")
        if not name or price < 1:
            return _json(422, success=False, message="Product name and a positive price are required.")
        supplier_id = str(body.get("supplierProductId", "")).strip() or None
        auto = bool(supplier_id)
        if not auto and quantity != len(stock_items):
            return _json(422, success=False, message="For manual products, provide one real delivery item per stock unit.")
        cur = service["db"].execute(
            "INSERT INTO products(name,description,price,active,supplier_product_id,supplier_price,auto_fulfill,category,supplier,supplier_stock,delivery_type,web_brand,web_logo_url,web_requirements,web_delivery_instructions,web_options_json,web_duration) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (name, str(body.get("description", ""))[:10000], price, 1 if (auto or quantity > 0) else 0,
             supplier_id, None, int(auto), str(body.get("category", "AI Tools"))[:80],
             str(body.get("supplier", "default"))[:80], quantity if auto else None,
             "auto" if auto else "manual", str(body.get("brand", ""))[:100],
             str(body.get("logoUrl", ""))[:1000], str(body.get("importantRequirements", ""))[:5000],
             str(body.get("howItIsDelivered", ""))[:5000], "[]", str(body.get("duration", ""))[:100]),
        )
        if stock_items:
            service["db"].executemany("INSERT INTO stock(product_id,payload,sold) VALUES(?,?,0)", [(cur.lastrowid, item.strip()) for item in stock_items])
        row = service["db"].execute("SELECT * FROM products WHERE id=?", (cur.lastrowid,)).fetchone()
        _log(service, claims["sub"], "Create product", f"Product {cur.lastrowid}")
        return _json(201, success=True, product=_product(service, row))

    async def admin_product_update(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        product_id = req.match_info["product_id"]
        row = service["db"].execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            return _json(404, success=False, message="Product not found.")
        try:
            body = await req.json()
            fields = {
                "name": ("name", 180), "description": ("description", 10000),
                "category": ("category", 80), "brand": ("web_brand", 100),
                "duration": ("web_duration", 100),
                "logoUrl": ("web_logo_url", 1000),
                "importantRequirements": ("web_requirements", 5000),
                "howItIsDelivered": ("web_delivery_instructions", 5000),
            }
            updates = []
            values = []
            for key, (column, limit) in fields.items():
                if key in body:
                    updates.append(f"{column}=?")
                    values.append(str(body[key]).strip()[:limit])
            if "usdPrice" in body or "price" in body:
                updates.append("price=?")
                values.append(round(float(body.get("usdPrice", body.get("price"))) * 100))
                updates.append("fixed_price=1")
            if "variants" in body:
                variants = body["variants"]
                if not isinstance(variants, list) or len(variants) > 30:
                    return _json(422, success=False, message="Product options are invalid.")
                for variant in variants:
                    if not isinstance(variant, dict) or not str(variant.get("duration", "")).strip():
                        return _json(422, success=False, message="Each option needs a name.")
                updates.append("web_options_json=?")
                values.append(json.dumps(variants))
        except (ValueError, TypeError, json.JSONDecodeError):
            return _json(422, success=False, message="Product changes are invalid.")
        if updates:
            values.append(product_id)
            service["db"].execute(f"UPDATE products SET {','.join(updates)} WHERE id=?", values)
        updated = service["db"].execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        _log(service, claims["sub"], "Update product", f"Product {product_id}")
        return _json(200, success=True, product=_product(service, updated))

    async def admin_product_stock(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        product_id = req.match_info["product_id"]
        row = service["db"].execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            return _json(404, success=False, message="Product not found.")
        try:
            body = await req.json()
            quantity = int(body.get("totalStock", 0))
            if quantity < 0 or quantity > 100000:
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            return _json(422, success=False, message="Stock quantity is invalid.")
        if row["supplier_product_id"] and row["auto_fulfill"]:
            service["db"].execute("UPDATE products SET supplier_stock=?,active=? WHERE id=?", (quantity, int(quantity > 0), product_id))
        else:
            return _json(422, success=False, message="Manual stock must be added as delivery items so fulfillment has real credentials.")
        updated = service["db"].execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        _log(service, claims["sub"], "Set supplier stock", f"Product {product_id}: {quantity}")
        return _json(200, success=True, product=_product(service, updated))

    async def admin_stock_adjust(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        try:
            delta = int((await req.json()).get("delta"))
        except (ValueError, TypeError, json.JSONDecodeError):
            return _json(422, success=False, message="Stock adjustment is invalid.")
        product_id = req.match_info["product_id"]
        row = service["db"].execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            return _json(404, success=False, message="Product not found.")
        if not row["supplier_product_id"] or not row["auto_fulfill"]:
            return _json(422, success=False, message="Manual stock must be managed with delivery items.")
        quantity = int(row["supplier_stock"] or 0) + delta
        if not 0 <= quantity <= 100000:
            return _json(422, success=False, message="Stock adjustment would result in an invalid quantity.")
        service["db"].execute("UPDATE products SET supplier_stock=?,active=? WHERE id=?", (quantity, int(quantity > 0), product_id))
        updated = service["db"].execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        _log(service, claims["sub"], "Adjust supplier stock", f"Product {product_id}: {delta:+d}")
        return _json(200, success=True, product=_product(service, updated))

    async def admin_stock_items(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        try:
            body = await req.json()
            product_id = int(req.match_info["product_id"])
            payloads = body.get("items", body.get("payload", ""))
            items = [line.strip() for line in payloads.splitlines() if line.strip()] if isinstance(payloads, str) else payloads
            replace = bool(body.get("replace"))
            if not isinstance(items, list) or (not items and not replace) or len(items) > 1000 or any(not isinstance(x, str) or not x.strip() for x in items):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            return _json(422, success=False, message="Add one or more non-empty delivery items.")
        exists = service["db"].execute("SELECT id FROM products WHERE id=?", (product_id,)).fetchone()
        if not exists:
            return _json(404, success=False, message="Product not found.")
        if replace:
            service["db"].execute("DELETE FROM stock WHERE product_id=? AND sold=0", (product_id,))
        if items:
            service["db"].executemany("INSERT INTO stock(product_id,payload,sold) VALUES(?,?,0)", [(product_id, x.strip()) for x in items])
        _log(service, claims["sub"], "Replace local stock" if replace else "Add local stock", f"Product {product_id}: {len(items)} delivery items")
        return _json(200, success=True, message=f"Added {len(items)} delivery items." if not replace else f"Replaced available stock with {len(items)} delivery items.")

    async def admin_toggle_product(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        try:
            body = await req.json()
            enabled = bool(body.get("enabled"))
        except json.JSONDecodeError:
            return _json(400, success=False, message="Invalid request body.")
        product_id = req.match_info["product_id"]
        service["db"].execute("UPDATE products SET active=? WHERE id=?", (int(enabled), product_id))
        row = service["db"].execute("SELECT * FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            return _json(404, success=False, message="Product not found.")
        _log(service, claims["sub"], "Toggle product", f"Product {product_id}: {'enabled' if enabled else 'disabled'}")
        return _json(200, success=True, product=_product(service, row))

    async def admin_product_delete(req):
        claims, error = _require_admin(req, "admin")
        if error:
            return error
        product_id = req.match_info["product_id"]
        row = service["db"].execute("SELECT name FROM products WHERE id=?", (product_id,)).fetchone()
        if not row:
            return _json(404, success=False, message="Product not found.")
        order_count = service["db"].execute("SELECT COUNT(*) FROM orders WHERE product_id=?", (product_id,)).fetchone()[0]
        if order_count:
            service["db"].execute("UPDATE products SET active=0 WHERE id=?", (product_id,))
            outcome = "disabled because order history exists"
        else:
            service["db"].execute("DELETE FROM stock WHERE product_id=?", (product_id,))
            service["db"].execute("DELETE FROM products WHERE id=?", (product_id,))
            outcome = "deleted"
        _log(service, claims["sub"], "Delete product", f"Product {product_id} {outcome}")
        return _json(200, success=True, message=f"Product {outcome}.")

    async def admin_order_action(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        row = _lookup_order(service, req.match_info["order_id"])
        if not row:
            return _json(404, success=False, message="Order not found.")
        action = req.match_info["action"]
        try:
            body = await req.json() if req.can_read_body else {}
        except json.JSONDecodeError:
            return _json(400, success=False, message="Invalid request body.")
        db = service["db"]
        if action == "approve":
            if row["status"] in ("refunded", "cancelled", "rejected", "completed"):
                return _json(409, success=False, message="Order cannot be approved in its current state.")
            if row["payment_method"] == "bachs":
                paid, verified = await service["verify_bachs_transaction"](row["invoice_id"] or "")
                if not paid:
                    return _json(409, success=False, message="Bachs has not confirmed this payment.")
                if verified.get("checkout_id") != row["invoice_id"]:
                    return _json(409, success=False, message="Verified Bachs checkout does not match the order.")
                try:
                    received = float(verified.get("amount"))
                    currency = str(verified.get("currency") or service["get_bachs_currency"]()).upper()
                    expected = int(row["price"] or 0) / 100
                    if currency == "NGN": expected *= service["get_usd_ngn_rate"]()
                    if received + (1 if currency == "NGN" else .01) < expected:
                        return _json(409, success=False, message="Verified payment is below the order total.")
                except (TypeError, ValueError):
                    return _json(409, success=False, message="Verified Bachs amount is missing.")
            elif row["payment_method"] == "cryptobot":
                paid = await service["check_cryptobot_invoice"](int(row["invoice_id"] or 0))
                if not paid:
                    return _json(409, success=False, message="CryptoBot has not confirmed this payment.")
            elif row["payment_method"] in ("bank", "binance"):
                if not row["txid"]:
                    return _json(409, success=False, message="Add and verify a payment reference before approval.")
                db.execute("UPDATE orders SET status='paid' WHERE id=?", (row["id"],))
            else:
                return _json(409, success=False, message="This payment method requires provider verification before fulfillment.")
            bot = req.app.get(TELEGRAM_BOT_KEY)
            payload, error_key = await service["fulfill_order"](bot, int(row["id"]))
            _log(service, claims["sub"], "Approve order", f"Order {row['id']}")
            updated = db.execute("SELECT * FROM orders WHERE id=?", (row["id"],)).fetchone()
            return _json(200, success=bool(payload) or updated["status"] == "manual_pending", order=_order(service, updated, True), message=error_key or "Order processed.")
        if action == "reject":
            db.execute("UPDATE orders SET status='rejected' WHERE id=? AND status!='completed'", (row["id"],))
            _log(service, claims["sub"], "Reject order", f"Order {row['id']}")
        elif action == "deliver":
            if row["status"] not in ("paid", "manual_pending", "completed"):
                return _json(409, success=False, message="Only paid orders can be delivered.")
            payload = str(body.get("payload", body.get("deliveredValue", ""))).strip()
            if not payload:
                return _json(422, success=False, message="Delivery content is required.")
            db.execute("UPDATE orders SET payload=?,status='completed' WHERE id=? AND refunded=0", (payload, row["id"]))
            _log(service, claims["sub"], "Deliver order", f"Order {row['id']}")
            await _send_customer_email(row["customer_email"], f"CreditLog order #CL-{row['id']} delivered", f"Your order for {row['product_name']} is ready.\n\nDelivery:\n{payload}")
        elif action == "refund":
            if row["status"] not in ("paid", "manual_pending", "completed", "refunded"):
                return _json(409, success=False, message="Only paid orders can be marked refunded.")
            db.execute("UPDATE orders SET refunded=1,status='refunded' WHERE id=? AND refunded=0", (row["id"],))
            _log(service, claims["sub"], "Mark order refunded", f"Order {row['id']}; payment-provider refund is not initiated by this action")
        else:
            return _json(404, success=False, message="Unknown order action.")
        updated = db.execute("SELECT * FROM orders WHERE id=?", (row["id"],)).fetchone()
        return _json(200, success=True, order=_order(service, updated, True))

    async def admin_customers(req):
        claims, error = _require_admin(req, "admin", "manager", "support")
        if error:
            return error
        rows = service["db"].execute("SELECT * FROM orders WHERE customer_email!='' ORDER BY id DESC").fetchall()
        customers = {}
        rate = service["get_usd_ngn_rate"]()
        for row in rows:
            email = row["customer_email"].lower()
            customer = customers.setdefault(email, {"email": email, "name": row["customer_name"], "phone": row["customer_phone"], "orderCount": 0, "totalSpentUsd": 0, "totalSpentNgn": 0, "products": [], "deliveryChannels": [row["delivery_channel"] or "Email"]})
            customer["orderCount"] += 1
            customer["totalSpentUsd"] += int(row["price"] or 0) / 100
            customer["products"].append(row["product_name"])
        for customer in customers.values():
            customer["totalSpentUsd"] = round(customer["totalSpentUsd"], 2)
            customer["totalSpentNgn"] = round(customer["totalSpentUsd"] * rate)
        return _json(200, success=True, count=len(customers), customers=list(customers.values()))

    async def admin_analytics(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        db = service["db"]
        total_orders = db.execute("SELECT COUNT(*) FROM orders").fetchone()[0]
        completed = db.execute("SELECT COUNT(*),COALESCE(SUM(price),0) FROM orders WHERE status='completed' AND refunded=0").fetchone()
        pending = db.execute("SELECT COUNT(*) FROM orders WHERE status IN ('pending','paid','manual_pending')").fetchone()[0]
        customers = db.execute("SELECT COUNT(DISTINCT lower(customer_email)) FROM orders WHERE customer_email!=''").fetchone()[0]
        rate = service["get_usd_ngn_rate"]()
        recent_rows = db.execute("SELECT * FROM orders ORDER BY id DESC LIMIT 8").fetchall()
        recent = [_order(service, row, True) for row in recent_rows]
        for order in recent:
            order["amountNgn"] = order["totalNgn"]
        points = []
        for offset in range(6, -1, -1):
            day = db.execute("SELECT date('now', ?)", (f"-{offset} days",)).fetchone()[0]
            day_row = db.execute("SELECT COUNT(*),COALESCE(SUM(price),0) FROM orders WHERE date(created)=? AND status='completed' AND refunded=0", (day,)).fetchone()
            points.append({"label": day[5:], "orders": day_row[0], "revenue": round(day_row[1] / 100 * rate)})
        max_revenue = max((point["revenue"] for point in points), default=0)
        chart = {"points": points, "maxRevenue": max_revenue or 1000, "yTicks": [max_revenue, max_revenue * .75, max_revenue * .5, max_revenue * .25] if max_revenue else [1000, 750, 500, 250]}
        return _json(200, success=True, totalOrders=total_orders, completedOrders=completed[0],
                     revenueUsd=round(completed[1] / 100, 2), pendingDeliveries=pending, totalCustomers=customers,
                     kpis={"totalSalesNgn": round(completed[1] / 100 * rate), "totalOrders": total_orders,
                           "activeCustomers": customers, "pendingDeliveries": pending},
                     chart=chart, recentOrders=recent)

    async def admin_settings(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        if req.method == "GET":
            keys = ["profit_margin", "usd_ngn_rate", "bachs_enabled", "bachs_base_url", "bachs_currency", "bachs_callback_url", "fsub_channels", "fsub_enabled", "fsub_check_admin", "fulfillment_mode", "featured_products", "broadcaster_enabled", "low_stock"]
            values = {key: _setting(service, key) for key in keys}
            values.update({"profit_margin": service["get_default_margin"](),
                           "usd_ngn_rate": service["get_usd_ngn_rate"](),
                           "bachs_enabled": "1" if service["is_bachs_enabled"]() else "0",
                           "bachs_base_url": service["get_bachs_base_url"](service["get_bachs_key"]()),
                           "bachs_currency": service["get_bachs_currency"](),
                           "bachs_callback_url": _setting(service, "bachs_callback_url", service.get("BACHS_CALLBACK_URL", "")),
                           "fsub_channels": ",".join(service["get_fsub_channels"]()),
                           "fsub_enabled": "1" if service["is_fsub_enabled"]() else "0",
                           "broadcaster_enabled": _setting(service, "broadcaster_enabled", "1"),
                           "fulfillment_mode": _setting(service, "fulfillment_mode", "auto")})
            values["low_stock"] = int(service["LOW_STOCK"])
            values["bankAccounts"] = service["get_bank_accounts"]()
            values["bachsConfigured"] = service["is_bachs_configured"]()
            values["bachsApiKeyMasked"] = "••••••••" if service["get_bachs_key"]() else ""
            values["exchangeRate"] = service["get_usd_ngn_rate"]()
            values["categories"] = [dict(x) for x in service["db"].execute("SELECT key,name,icon,sort_order FROM categories ORDER BY sort_order,rowid").fetchall()]
            values["featuredProducts"] = service["get_featured_product_ids"]()
            values.update({"cryptoBotConfigured": bool(service.get("CRYPTOBOT_TOKEN")),
                           "cryptoBotTokenMasked": "••••••••" if service.get("CRYPTOBOT_TOKEN") else "",
                           "binancePayId": service.get("BINANCE_PAY_ID", ""),
                           "cryptoUsdtTrc20": service.get("CRYPTO_USDT_TRC20", ""),
                           "cryptoUsdtBep20": service.get("CRYPTO_USDT_BEP20", ""),
                           "supportUrl": service.get("SUPPORT_URL", ""),
                           "channelUrl": service.get("CHANNEL_URL", "")})
            return _json(200, success=True, settings=values)
        if claims["role"] not in ("admin", "manager"):
            return _json(403, success=False, message="Settings cannot be changed by this account.")
        try:
            body = await req.json()
        except json.JSONDecodeError:
            return _json(400, success=False, message="Invalid request body.")
        key_map = {"profit_margin": ("profit_margin", int, 0, 500), "usdToNgnRate": ("usd_ngn_rate", int, 1, 100000), "usd_ngn_rate": ("usd_ngn_rate", int, 1, 100000), "bachs_currency": ("bachs_currency", str, 1, 3), "bachs_base_url": ("bachs_base_url", str, 1, 500), "bachs_callback_url": ("bachs_callback_url", str, 0, 1000), "fsub_channels": ("fsub_channels", str, 0, 2000), "fulfillment_mode": ("fulfillment_mode", str, 0, 20), "featured_products": ("featured_products", str, 0, 2000)}
        try:
            for key, value in body.items():
                if key in key_map:
                    target, caster, minimum, maximum = key_map[key]
                    converted = caster(value)
                    if caster is int and not minimum <= converted <= maximum:
                        return _json(422, success=False, message=f"Invalid value for {key}.")
                    if caster is str and len(converted) > maximum:
                        return _json(422, success=False, message=f"Invalid value for {key}.")
                    if target == "fulfillment_mode" and converted not in ("auto", "manual", "instant"):
                        return _json(422, success=False, message="Fulfillment mode must be auto, manual, or instant.")
                    if target == "bachs_currency" and converted.upper() not in ("USD", "NGN", "GHS", "KES", "ZAR", "EUR", "GBP"):
                        return _json(422, success=False, message="Unsupported Bachs currency.")
                    service["set_setting"](target, converted.upper() if target == "bachs_currency" else str(converted))
                elif key == "bachs_enabled" and isinstance(value, bool):
                    service["set_setting"]("bachs_enabled", "1" if value else "0")
                elif key == "fsub_enabled" and isinstance(value, bool):
                    service["set_fsub_enabled"](value)
                elif key == "fsub_check_admin" and isinstance(value, bool):
                    service["set_setting"]("fsub_check_admin", "1" if value else "0")
                elif key == "broadcaster_enabled" and isinstance(value, bool):
                    service["set_setting"]("broadcaster_enabled", "1" if value else "0")
                elif key == "bachs_api_key" and isinstance(value, str) and len(value) <= 500:
                    service["set_setting"]("bachs_api_key", value.strip())
                elif key in ("cryptobot_token", "binance_pay_id", "crypto_usdt_trc20", "crypto_usdt_bep20", "support_url", "channel_url") and isinstance(value, str):
                    target = key
                    cleaned = value.strip()[:1000]
                    if key in ("support_url", "channel_url") and cleaned and not cleaned.startswith(("https://", "http://", "tg://")):
                        return _json(422, success=False, message=f"{key} must be a valid link.")
                    service["set_setting"](target, cleaned)
                    variables = {"cryptobot_token": "CRYPTOBOT_TOKEN", "binance_pay_id": "BINANCE_PAY_ID", "crypto_usdt_trc20": "CRYPTO_USDT_TRC20", "crypto_usdt_bep20": "CRYPTO_USDT_BEP20", "support_url": "SUPPORT_URL", "channel_url": "CHANNEL_URL"}
                    service[variables[key]] = cleaned
                elif key == "low_stock":
                    threshold = int(value)
                    if not 0 <= threshold <= 10000:
                        return _json(422, success=False, message="Low-stock threshold is invalid.")
                    service["LOW_STOCK"] = threshold
                    service["set_setting"]("low_stock", str(threshold))
                elif key == "bankAccounts" and isinstance(value, list) and len(value) <= 20:
                    normalized = []
                    for account in value:
                        if not isinstance(account, dict):
                            return _json(422, success=False, message="Bank account settings are invalid.")
                        normalized.append({"bank_name": str(account.get("bank_name", account.get("bankName", "")))[:100], "account_number": str(account.get("account_number", account.get("accountNumber", "")))[:40], "account_name": str(account.get("account_name", account.get("accountName", "")))[:160]})
                    service["save_bank_accounts"](normalized)
        except (TypeError, ValueError):
            return _json(422, success=False, message="Settings are invalid.")
        _log(service, claims["sub"], "Update store settings", ", ".join(k for k in body if "key" not in k.lower()))
        return await admin_settings(req.clone(method="GET"))

    async def admin_supplier_status(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        balances = await service["supplier_registry"].get_all_balances()
        vendors = _supplier_vendor_list(service)
        return _json(200, success=True, vendors=vendors, balance=balances, botDbConnected=True,
                     settings={"mode": "live", "baseUrl": service["supplier_registry"].get().base_url,
                               "markupPercent": service["get_default_margin"](),
                               "usdToNgnRate": service["get_usd_ngn_rate"](),
                               "lowBalanceThreshold": 0, "autoFulfill": True},
                     syncStatus={"lastStatus": "available"})

    async def admin_supplier_vendors(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        registry = service["supplier_registry"]
        if req.method == "GET":
            return _json(200, success=True, vendors=_supplier_vendor_list(service))
        try:
            body = await req.json()
            name = str(body.get("name", "")).strip()[:100]
            vendor_id = str(body.get("id") or name.lower().replace(" ", "-")).strip().lower()[:48]
            base_url = str(body.get("baseUrl", "")).strip().rstrip("/")
            if not name or not re.fullmatch(r"[a-z0-9_-]+", vendor_id) or not base_url.startswith("https://"):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            return _json(422, success=False, message="Supplier name, ID, and HTTPS API URL are required.")
        configs = _supplier_configs(service)
        existing = next((item for item in configs if item["id"] == vendor_id), None)
        key = str(body.get("apiKey", "")).strip() or (existing or {}).get("apiKey", "")
        if not key and vendor_id == "default":
            key = registry.get("default").api_key
        if not key:
            return _json(422, success=False, message="Enter the supplier API key before saving.")
        saved = {"id": vendor_id, "name": name, "baseUrl": base_url, "apiKey": key,
                 "isDefault": bool(body.get("isDefault")) or not registry.list_suppliers()}
        if saved["isDefault"]:
            for item in configs:
                item["isDefault"] = False
        configs = [item for item in configs if item["id"] != vendor_id] + [saved]
        service["set_setting"]("web_supplier_configs", json.dumps(configs))
        _apply_supplier_configs(service, configs)
        _log(service, claims["sub"], "Save supplier", f"Supplier {vendor_id}")
        vendors = _supplier_vendor_list(service)
        return _json(200, success=True, vendors=vendors)

    async def admin_supplier_vendor_default(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        vendor_id = req.match_info["vendor_id"]
        configs = _supplier_configs(service)
        if vendor_id not in service["supplier_registry"].list_suppliers():
            return _json(404, success=False, message="Supplier not found.")
        for item in configs:
            item["isDefault"] = item["id"] == vendor_id
        service["supplier_registry"]._default_name = vendor_id
        service["set_setting"]("web_supplier_configs", json.dumps(configs))
        _log(service, claims["sub"], "Set default supplier", vendor_id)
        return _json(200, success=True, vendors=_supplier_vendor_list(service))

    async def admin_supplier_vendor_delete(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        vendor_id = req.match_info["vendor_id"]
        if vendor_id == service["supplier_registry"].default_name():
            return _json(409, success=False, message="Set another default supplier before removing this one.")
        linked = service["db"].execute("SELECT COUNT(*) FROM products WHERE supplier=?", (vendor_id,)).fetchone()[0]
        if linked:
            return _json(409, success=False, message="This supplier has linked products. Disable or remap them before removing it.")
        configs = [item for item in _supplier_configs(service) if item["id"] != vendor_id]
        service["set_setting"]("web_supplier_configs", json.dumps(configs))
        service["supplier_registry"]._suppliers.pop(vendor_id, None)
        _log(service, claims["sub"], "Remove supplier", vendor_id)
        return _json(200, success=True, vendors=_supplier_vendor_list(service))

    async def admin_supplier_vendor_test(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        vendor_id = req.match_info["vendor_id"]
        client = service["supplier_registry"].get(vendor_id)
        if not client or not client.api_key:
            return _json(404, success=False, message="Supplier is not configured.")
        try:
            balance = await client.get_balance()
            info = await client.get_info()
            result = {"vendor": {"id": vendor_id, "name": vendor_id, "mode": "live"}, "balance": balance, "info": info}
            _log(service, claims["sub"], "Test supplier connection", vendor_id)
            return _json(200, success=True, result=result)
        except Exception:
            return _json(502, success=False, message="Supplier connection test failed.")

    async def admin_supplier_settings(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        if req.method == "GET":
            current = service["supplier_registry"].get()
            return _json(200, success=True, settings={"mode": "live", "baseUrl": current.base_url, "markupPercent": service["get_default_margin"](), "usdToNgnRate": service["get_usd_ngn_rate"](), "autoFulfill": True})
        try:
            body = await req.json()
            if "markupPercent" in body:
                margin = int(body["markupPercent"])
                if not 0 <= margin <= 500: raise ValueError
                service["set_setting"]("profit_margin", str(margin))
            if "usdToNgnRate" in body:
                service["set_usd_ngn_rate"](int(body["usdToNgnRate"]))
            if body.get("baseUrl") or body.get("apiKey"):
                current = service["supplier_registry"].get("default")
                base_url = str(body.get("baseUrl") or current.base_url).strip().rstrip("/")
                api_key = str(body.get("apiKey") or current.api_key).strip()
                if not base_url.startswith("https://") or not api_key: raise ValueError
                configs = _supplier_configs(service)
                saved = next((item for item in configs if item["id"] == service["supplier_registry"].default_name()), None)
                if saved:
                    saved.update({"baseUrl": base_url, "apiKey": api_key})
                else:
                    saved = {"id": service["supplier_registry"].default_name(), "name": service["supplier_registry"].default_name(), "baseUrl": base_url, "apiKey": api_key, "isDefault": True}
                    configs.append(saved)
                service["set_setting"]("web_supplier_configs", json.dumps(configs))
                current.base_url, current.api_key = base_url, api_key
        except (ValueError, TypeError, json.JSONDecodeError):
            return _json(422, success=False, message="Supplier settings are invalid.")
        _log(service, claims["sub"], "Update supplier settings", "Pricing, exchange rate, or default supplier")
        current = service["supplier_registry"].get()
        return _json(200, success=True, settings={"mode": "live", "baseUrl": current.base_url, "markupPercent": service["get_default_margin"](), "usdToNgnRate": service["get_usd_ngn_rate"](), "autoFulfill": True})

    async def admin_supplier_apply_multipliers(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        try:
            result = await service["perform_supplier_sync"]()
        except Exception:
            return _json(502, success=False, message="Supplier catalog synchronization failed.")
        _log(service, claims["sub"], "Apply supplier pricing", f"Updated {result[1]} supplier products")
        return _json(200, success=True, updatedCount=result[1], sync={"newProductsAdded": result[0], "pricesUpdated": result[1], "errors": result[3]})

    async def admin_supplier_sync_status(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        return _json(200, success=True, syncStatus={"lastStatus": "available", "sharedDatabase": True})

    async def admin_clear_cache(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        result = await service["perform_supplier_sync"]()
        _log(service, claims["sub"], "Clear supplier catalog cache", f"Added {result[0]}, updated {result[1]}")
        return _json(200, success=True, message="Supplier catalog synchronized.", sync={"newProductsAdded": result[0], "pricesUpdated": result[1], "errors": result[3]})

    async def admin_supplier_test_order(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        return _json(409, success=False, message="A supplier test order may incur a charge. Use Test Connection to check the API without placing an order.")

    async def admin_supplier_products(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        vendor_id = req.query.get("vendorId")
        client = service["supplier_registry"].get(vendor_id) if vendor_id else service["supplier_registry"].get()
        if not client:
            return _json(404, success=False, message="Supplier not found.")
        try:
            catalog = await client.get_products()
        except Exception:
            return _json(502, success=False, message="Supplier catalog is unavailable.")
        return _json(200, success=True, count=len(catalog), products=catalog)

    async def admin_supplier_sync(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        try:
            result = await service["perform_supplier_sync"]()
        except Exception:
            return _json(502, success=False, message="Supplier synchronization failed.")
        _log(service, claims["sub"], "Supplier sync", f"Added {result[0]}, updated {result[1]}, total {result[2]}")
        return _json(200, success=True, sync={"newProductsAdded": result[0], "pricesUpdated": result[1], "totalSupplierProducts": result[2], "errors": result[3], "botDbSynced": True})

    async def admin_categories(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        if req.method == "GET":
            rows = service["db"].execute("SELECT key,name,icon,sort_order FROM categories ORDER BY sort_order,rowid").fetchall()
            return _json(200, success=True, categories=[dict(x) for x in rows])
        if req.method == "DELETE":
            key = req.query.get("key", "").strip().lower()
            if not key:
                return _json(422, success=False, message="Category key is required.")
            category = service["db"].execute("SELECT name FROM categories WHERE key=?", (key,)).fetchone()
            if not category:
                return _json(404, success=False, message="Category not found.")
            linked = service["db"].execute("SELECT COUNT(*) FROM products WHERE lower(category) IN (lower(?),lower(?))", (key, category["name"])).fetchone()[0]
            if linked:
                return _json(409, success=False, message="Move products to another category before removing this category.")
            service["db"].execute("DELETE FROM categories WHERE key=?", (key,))
            _log(service, claims["sub"], "Delete category", key)
            rows = service["db"].execute("SELECT key,name,icon,sort_order FROM categories ORDER BY sort_order,rowid").fetchall()
            return _json(200, success=True, categories=[dict(x) for x in rows])
        try:
            body = await req.json()
            key = str(body.get("key", "")).strip().lower()[:60]
            name = str(body.get("name", "")).strip()[:100]
            icon = str(body.get("icon", "📦"))[:16]
            sort_order = int(body.get("sortOrder", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            return _json(422, success=False, message="Category settings are invalid.")
        if not key or not name:
            return _json(422, success=False, message="Category key and name are required.")
        service["db"].execute("INSERT INTO categories(key,name,icon,sort_order) VALUES(?,?,?,?) ON CONFLICT(key) DO UPDATE SET name=excluded.name,icon=excluded.icon,sort_order=excluded.sort_order", (key, name, icon, sort_order))
        _log(service, claims["sub"], "Save category", key)
        return _json(200, success=True, categories=[dict(x) for x in service["db"].execute("SELECT key,name,icon,sort_order FROM categories ORDER BY sort_order,rowid").fetchall()])

    async def admin_featured(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        if req.method == "GET":
            return _json(200, success=True, featuredProducts=service["get_featured_product_ids"]())
        try:
            body = await req.json()
            products = body.get("productIds", [])
            if not isinstance(products, list) or len(products) > 100:
                raise ValueError
            normalized = [int(x) for x in products]
        except (ValueError, TypeError, json.JSONDecodeError):
            return _json(422, success=False, message="Featured product list is invalid.")
        service["set_featured_product_ids"](normalized)
        _log(service, claims["sub"], "Update featured products", f"{len(normalized)} items")
        return _json(200, success=True, featuredProducts=service["get_featured_product_ids"]())

    async def admin_users(req):
        claims, error = _require_admin(req, "admin")
        if error:
            return error
        if req.method == "GET":
            rows = service["db"].execute("SELECT username,role,active,created_at FROM web_admins ORDER BY username").fetchall()
            return _json(200, success=True, users=[dict(x) for x in rows])
        if req.method == "DELETE":
            username = req.query.get("username", "").strip().lower()
            target = service["db"].execute("SELECT role,active FROM web_admins WHERE lower(username)=?", (username,)).fetchone()
            if not target:
                return _json(404, success=False, message="Administrator not found.")
            if username == claims["sub"].lower():
                return _json(409, success=False, message="You cannot disable your own account.")
            if target["role"] == "admin" and target["active"] and service["db"].execute("SELECT COUNT(*) FROM web_admins WHERE active=1 AND role='admin'").fetchone()[0] <= 1:
                return _json(409, success=False, message="The last active administrator cannot be disabled.")
            service["db"].execute("UPDATE web_admins SET active=0 WHERE lower(username)=?", (username,))
            _log(service, claims["sub"], "Disable web administrator", username)
            rows = service["db"].execute("SELECT username,role,active,created_at FROM web_admins ORDER BY username").fetchall()
            return _json(200, success=True, users=[dict(x) for x in rows])
        try:
            body = await req.json()
            username = str(body.get("username", "")).strip().lower()[:254]
            password = str(body.get("password", ""))
            role = str(body.get("role", "support"))
            if not username or "@" not in username or len(password) < 12 or role not in ("admin", "manager", "support"):
                raise ValueError
        except (ValueError, TypeError, json.JSONDecodeError):
            return _json(422, success=False, message="Use a valid email, a password of at least 12 characters, and an allowed role.")
        service["db"].execute("INSERT INTO web_admins(username,password_hash,role,active,created_at) VALUES(?,?,?,?,?) ON CONFLICT(username) DO UPDATE SET password_hash=excluded.password_hash,role=excluded.role,active=1", (username, _password_hash(password), role, 1, datetime.now(timezone.utc).isoformat()))
        _log(service, claims["sub"], "Save web admin", f"{username} ({role})")
        return await admin_users(req.clone(method="GET"))

    async def admin_export(req):
        claims, error = _require_admin(req, "admin", "manager")
        if error:
            return error
        kind = req.match_info["kind"]
        if kind == "backup":
            target = sqlite3.connect(":memory:")
            service["db"].backup(target)
            payload = target.serialize()
            target.close()
            filename = "creditlog-backup.db"
            content_type = "application/octet-stream"
        elif kind == "products.csv":
            rows = service["db"].execute("SELECT id,name,description,price,active,category,supplier,supplier_product_id,supplier_stock FROM products ORDER BY id").fetchall()
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["id", "name", "description", "price_cents", "active", "category", "supplier", "supplier_product_id", "supplier_stock"])
            writer.writerows([tuple(row) for row in rows])
            payload = output.getvalue().encode("utf-8-sig")
            filename = "creditlog-products.csv"
            content_type = "text/csv"
        elif kind == "orders.csv":
            rows = service["db"].execute("SELECT id,product_name,price,quantity,payment_method,status,txid,invoice_id,customer_name,customer_email,customer_phone,created FROM orders ORDER BY id DESC").fetchall()
            output = io.StringIO()
            writer = csv.writer(output)
            writer.writerow(["id", "product", "price_cents", "quantity", "payment_method", "status", "transaction_reference", "invoice_id", "customer_name", "customer_email", "customer_phone", "created"])
            writer.writerows([tuple(row) for row in rows])
            payload = output.getvalue().encode("utf-8-sig")
            filename = "creditlog-orders.csv"
            content_type = "text/csv"
        else:
            return _json(404, success=False, message="Unknown export type.")
        _log(service, claims["sub"], "Download data export", filename)
        return web.Response(body=payload, content_type=content_type, headers={"Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "no-store"})

    app.router.add_get("/api/admin/products", admin_inventory)
    app.router.add_post("/api/admin/products", admin_product_create)
    app.router.add_put("/api/admin/products/{product_id}", admin_product_update)
    app.router.add_put("/api/admin/products/{product_id}/stock", admin_product_stock)
    app.router.add_put("/api/admin/products/{product_id}/price", admin_product_update)
    app.router.add_post("/api/admin/products/{product_id}/stock-adjust", admin_stock_adjust)
    app.router.add_post("/api/admin/products/{product_id}/toggle-status", admin_toggle_product)
    app.router.add_put("/api/admin/products/{product_id}/variants", admin_product_update)
    app.router.add_post("/api/admin/products/{product_id}/stock-items", admin_stock_items)
    app.router.add_delete("/api/admin/products/{product_id}", admin_product_delete)
    app.router.add_post("/api/admin/orders/{order_id}/{action:approve|reject|deliver|refund}", admin_order_action)
    app.router.add_get("/api/admin/customers", admin_customers)
    app.router.add_get("/api/admin/analytics", admin_analytics)
    app.router.add_route("*", "/api/admin/settings", admin_settings)
    app.router.add_route("*", "/api/admin/categories", admin_categories)
    app.router.add_route("*", "/api/admin/featured", admin_featured)
    app.router.add_route("*", "/api/admin/users", admin_users)
    app.router.add_get(r"/api/admin/export/{kind:backup|products\.csv|orders\.csv}", admin_export)
    app.router.add_get("/api/admin/reseller/status", admin_supplier_status)
    app.router.add_get("/api/admin/reseller/products", admin_supplier_products)
    app.router.add_post("/api/admin/reseller/sync", admin_supplier_sync)
    app.router.add_route("*", "/api/admin/reseller/vendors", admin_supplier_vendors)
    app.router.add_post("/api/admin/reseller/vendors/{vendor_id}/test", admin_supplier_vendor_test)
    app.router.add_post("/api/admin/reseller/vendors/{vendor_id}/default", admin_supplier_vendor_default)
    app.router.add_delete("/api/admin/reseller/vendors/{vendor_id}", admin_supplier_vendor_delete)
    app.router.add_route("*", "/api/admin/reseller/settings", admin_supplier_settings)
    app.router.add_post("/api/admin/reseller/apply-multipliers", admin_supplier_apply_multipliers)
    app.router.add_get("/api/admin/reseller/sync/status", admin_supplier_sync_status)
    app.router.add_post("/api/admin/reseller/test-order", admin_supplier_test_order)
    app.router.add_post("/api/admin/clear-cache", admin_clear_cache)

    app.router.add_get("/api/products", products)
    app.router.add_get("/api/products/{product_id}", product_detail)
    app.router.add_post("/api/orders/quote", quote)
    app.router.add_post("/api/orders/create", create_order)
    app.router.add_get("/api/orders/track", track_order)
    app.router.add_post("/api/orders/{order_id}/payment-reference", submit_payment_reference)
    app.router.add_post("/api/payment/verify", payment_verify)
    app.router.add_post("/api/webhook/bachs", bachs_webhook)
    app.router.add_post("/api/admin/login", login)
    app.router.add_get("/api/admin/inventory", admin_inventory)
    app.router.add_get("/api/admin/orders", admin_orders)
    app.router.add_get("/api/admin/logs", admin_logs)

