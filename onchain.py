"""
On-Chain Payment Verifier for USDT BEP-20 (BNB Smart Chain) and TRC-20 (Tron Network).
Automatically confirms blockchain transactions and validates recipient, token contract, and transfer amount.
"""

import logging
import os
from typing import List, Optional, Tuple
import aiohttp

log = logging.getLogger("onchain")

# Official USDT Token Contracts
USDT_BEP20_CONTRACT = "0x55d398326f99059fF775485246999027B3197955".lower()
USDT_TRC20_CONTRACT = "TR7NHqjeKQxGTCi8q8ZY4pL8otSzgjLj6t"

# Public BSC RPC nodes (with fallbacks)
BSC_RPC_ENDPOINTS = [
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed1.ninicoin.io",
    "https://rpc.ankr.com/bsc",
]

# TronGrid API
TRONGRID_EVENTS_URL = "https://api.trongrid.io/v1/transactions/{hash}/events"
TRONGRID_TX_URL = "https://api.trongrid.io/wallet/gettransactioninfobyid"


def tron_base58_to_hex_variants(base58_addr: str) -> List[str]:
    """Returns normalized representations (base58, 0x-hex, 41-hex) for a Tron address."""
    base58_addr = base58_addr.strip()
    variants = [base58_addr.lower()]
    try:
        ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        val = 0
        for char in base58_addr:
            idx = ALPHABET.find(char)
            if idx == -1:
                return variants
            val = val * 58 + idx
        raw = val.to_bytes(25, byteorder="big")
        addr_hex = raw[1:21].hex().lower()
        variants.append("0x" + addr_hex)
        variants.append("41" + addr_hex)
        variants.append(addr_hex)
    except Exception:
        pass
    return variants


async def verify_bep20_usdt(
    tx_hash: str,
    expected_recipient: str,
    expected_usd: float,
    session: Optional[aiohttp.ClientSession] = None,
) -> Tuple[bool, str, Optional[float]]:
    """
    Verifies a BSC (BEP-20) USDT Transfer event via JSON-RPC.
    Returns (is_valid, message, detected_amount).
    """
    tx_hash = tx_hash.strip()
    if not tx_hash.startswith("0x"):
        tx_hash = "0x" + tx_hash

    if len(tx_hash) != 66:
        return False, "Invalid BEP-20 transaction hash format (must be 66 hex characters).", None

    expected_recipient = expected_recipient.strip().lower()
    if not expected_recipient.startswith("0x") or len(expected_recipient) != 42:
        return False, "Admin BEP-20 wallet address is misconfigured.", None

    payload = {
        "jsonrpc": "2.0",
        "method": "eth_getTransactionReceipt",
        "params": [tx_hash],
        "id": 1,
    }

    close_session = False
    if session is None or session.closed:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        close_session = True

    try:
        receipt = None
        for endpoint in BSC_RPC_ENDPOINTS:
            try:
                async with session.post(endpoint, json=payload) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        receipt = data.get("result")
                        if receipt:
                            break
            except Exception as e:
                log.warning("BSC RPC endpoint %s failed: %s", endpoint, e)
                continue

        if not receipt:
            return False, "Transaction not found on BNB Smart Chain yet. Please ensure the transaction has confirmed.", None

        # Verify receipt status (0x1 = success)
        status = receipt.get("status")
        if status != "0x1":
            return False, f"Transaction failed or reverted on-chain (status: {status}).", None

        # Check logs for ERC-20 Transfer event:
        # Transfer(address indexed from, address indexed to, uint256 value)
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        detected_amount = None
        matched_recipient = False

        for log_entry in receipt.get("logs", []):
            contract_addr = str(log_entry.get("address", "")).lower()
            topics = log_entry.get("topics", [])

            if contract_addr == USDT_BEP20_CONTRACT and len(topics) >= 3:
                if str(topics[0]).lower() == transfer_topic:
                    # topics[2] is 32-byte zero-padded recipient address
                    recipient_in_log = "0x" + topics[2][-40:].lower()
                    if recipient_in_log == expected_recipient:
                        matched_recipient = True
                        data_hex = log_entry.get("data", "0x0")
                        try:
                            raw_units = int(data_hex, 16)
                            detected_amount = raw_units / (10**18)
                        except (ValueError, TypeError):
                            pass
                        break

        if not matched_recipient:
            return False, "Transaction recipient does not match your configured BEP-20 wallet.", None

        if detected_amount is None:
            return False, "Could not determine USDT transfer amount from transaction logs.", None

        # Allow 0.01 tolerance for minor exchange precision
        if detected_amount < (expected_usd - 0.01):
            return False, f"Transfer amount (${detected_amount:.2f} USDT) is less than required (${expected_usd:.2f} USDT).", detected_amount

        return True, "BEP-20 payment verified successfully on-chain.", detected_amount

    except Exception as e:
        log.exception("Error verifying BEP-20 transaction %s: %s", tx_hash, e)
        return False, f"Blockchain verification error: {e}", None
    finally:
        if close_session:
            await session.close()


async def verify_trc20_usdt(
    tx_hash: str,
    expected_recipient: str,
    expected_usd: float,
    session: Optional[aiohttp.ClientSession] = None,
) -> Tuple[bool, str, Optional[float]]:
    """
    Verifies a Tron (TRC-20) USDT Transfer event via TronGrid API.
    Returns (is_valid, message, detected_amount).
    """
    tx_hash = tx_hash.strip()
    if tx_hash.startswith("0x"):
        tx_hash = tx_hash[2:]

    if len(tx_hash) != 64:
        return False, "Invalid TRC-20 transaction hash format (must be 64 hex characters).", None

    expected_recipient = expected_recipient.strip()

    close_session = False
    if session is None or session.closed:
        session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
        close_session = True

    try:
        events_url = TRONGRID_EVENTS_URL.format(hash=tx_hash)
        async with session.get(events_url, headers={"Accept": "application/json"}) as resp:
            if resp.status != 200:
                return False, f"TronGrid query failed with HTTP {resp.status}.", None
            data = await resp.json()

        events = data.get("data", [])
        if not events:
            return False, "No transfer events found for this Tron transaction yet. Please wait a moment and try again.", None

        matched_recipient = False
        detected_amount = None
        expected_variants = tron_base58_to_hex_variants(expected_recipient)

        for ev in events:
            ev_name = ev.get("event_name")
            contract_addr = ev.get("contract_address")
            res_dict = ev.get("result", {})

            if ev_name == "Transfer" and contract_addr == USDT_TRC20_CONTRACT:
                to_addr = res_dict.get("to")
                if to_addr and str(to_addr).strip().lower() in expected_variants:
                    matched_recipient = True
                    try:
                        raw_units = int(res_dict.get("value", 0))
                        amt = raw_units / 1_000_000.0  # USDT TRC20 has 6 decimals
                        detected_amount = (detected_amount or 0.0) + amt
                    except (ValueError, TypeError):
                        pass

        if not matched_recipient:
            return False, "Transaction recipient does not match your configured TRC-20 wallet.", None

        if detected_amount is None:
            return False, "Could not determine USDT transfer amount from Tron transaction.", None

        if detected_amount < (expected_usd - 0.01):
            return False, f"Transfer amount (${detected_amount:.2f} USDT) is less than required (${expected_usd:.2f} USDT).", detected_amount

        return True, "TRC-20 payment verified successfully on-chain.", detected_amount

    except Exception as e:
        log.exception("Error verifying TRC-20 transaction %s: %s", tx_hash, e)
        return False, f"Tron verification error: {e}", None
    finally:
        if close_session:
            await session.close()
