"""
Anchor BaaS client — Admerce.

Wraps the Anchor API for sub-account creation, book transfers, NIP
transfers, counterparties, and webhook verification.

When you obtain your Anchor API key:
  1. Set ANCHOR_API_KEY, ANCHOR_BASE_URL, ANCHOR_FBO_ACCOUNT_ID env vars
  2. Replace each `raise NotImplementedError` with the real HTTP call
  3. The rest of the codebase works unchanged
"""
from __future__ import annotations

import os
import hmac
import hashlib
import logging
from typing import Optional

logger = logging.getLogger(__name__)

ANCHOR_API_KEY        = os.getenv("ANCHOR_API_KEY", "").strip()
ANCHOR_BASE_URL       = os.getenv("ANCHOR_BASE_URL", "https://api.getanchor.co").rstrip("/")
ANCHOR_FBO_ACCOUNT_ID = os.getenv("ANCHOR_FBO_ACCOUNT_ID", "").strip()
ANCHOR_WEBHOOK_SECRET = os.getenv("ANCHOR_WEBHOOK_SECRET", "").strip()


class AnchorNotConfigured(RuntimeError):
    """Raised when an Anchor call is attempted but credentials are missing."""


def is_configured() -> bool:
    return bool(ANCHOR_API_KEY and ANCHOR_FBO_ACCOUNT_ID)


def _require_config():
    if not is_configured():
        raise AnchorNotConfigured(
            "Anchor is not configured. Set ANCHOR_API_KEY and "
            "ANCHOR_FBO_ACCOUNT_ID env vars."
        )


# ═══════════════════════════════════════════════════════════════
# HTTP plumbing — fill these when you get the API
# ═══════════════════════════════════════════════════════════════
async def _request(method: str, path: str, json_body: Optional[dict] = None) -> dict:
    """
    Single HTTP entry point. Implement this with httpx.AsyncClient
    when Anchor creds arrive. All other functions call this.
    """
    _require_config()

    # TODO(anchor): replace with real HTTP call
    #
    # import httpx
    # async with httpx.AsyncClient(timeout=30) as client:
    #     resp = await client.request(
    #         method, f"{ANCHOR_BASE_URL}{path}",
    #         headers={
    #             "x-anchor-key": ANCHOR_API_KEY,
    #             "Content-Type": "application/json",
    #             "Accept": "application/json",
    #         },
    #         json=json_body,
    #     )
    #     resp.raise_for_status()
    #     return resp.json()
    raise NotImplementedError(
        f"Anchor HTTP not yet wired. Path would be: {method} {path}"
    )


# ═══════════════════════════════════════════════════════════════
# Public API
# ═══════════════════════════════════════════════════════════════

async def create_customer(
    *,
    email: str,
    name: str,
    phone: Optional[str] = None,
    bvn: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> dict:
    """
    Create an IndividualCustomer on Anchor.
    Returns: {"customer_id": "..."}
    """
    payload = {
        "data": {
            "type": "IndividualCustomer",
            "attributes": {
                "email": email,
                "name": name,
                "phoneNumber": phone,
                "bvn": bvn,
                "metadata": metadata or {},
            },
        }
    }
    return await _request("POST", "/api/v1/customers", payload)


async def create_sub_account(
    *,
    customer_id: str,
    metadata: Optional[dict] = None,
) -> dict:
    """
    Create a sub-account under the Admerce FBO with its own virtual NUBAN.
    Returns: {"sub_account_id": "...", "virtual_nuban": "0123456789", "bank_name": "..."}
    """
    payload = {
        "data": {
            "type": "SubAccount",
            "attributes": {
                "createVirtualNuban": True,
                "metadata": metadata or {},
            },
            "relationships": {
                "parentAccount": {
                    "data": {"type": "DepositAccount", "id": ANCHOR_FBO_ACCOUNT_ID}
                },
                "customer": {
                    "data": {"type": "IndividualCustomer", "id": customer_id}
                },
            },
        }
    }
    return await _request("POST", "/api/v1/sub-accounts", payload)


async def get_sub_account_balance(sub_account_id: str) -> dict:
    """
    Returns: {"balance": 50000.00, "available": 50000.00, "currency": "NGN"}
    """
    return await _request("GET", f"/api/v1/sub-accounts/{sub_account_id}")


async def book_transfer(
    *,
    from_sub_account_id: str,
    to_sub_account_id: str,
    amount: float,
    reference: str,
    reason: str,
) -> dict:
    """
    Move money between two Admerce sub-accounts. Free, instant.
    Returns: {"transfer_id": "...", "status": "success"}
    """
    payload = {
        "data": {
            "type": "BookTransfer",
            "attributes": {
                "amount": amount,
                "currency": "NGN",
                "reason": reason,
                "reference": reference,
            },
            "relationships": {
                "account": {
                    "data": {"type": "SubAccount", "id": from_sub_account_id}
                },
                "destinationAccount": {
                    "data": {"type": "SubAccount", "id": to_sub_account_id}
                },
            },
        }
    }
    return await _request("POST", "/api/v1/transfers", payload)


async def create_counterparty(
    *,
    bank_code: str,
    account_number: str,
    account_name: Optional[str] = None,
) -> dict:
    """
    Cache a counterparty (external bank account) for NIP transfers.
    Returns: {"counterparty_id": "..."}
    """
    payload = {
        "data": {
            "type": "CounterParty",
            "attributes": {
                "bankCode": bank_code,
                "accountNumber": account_number,
                "accountName": account_name,
            },
        }
    }
    return await _request("POST", "/api/v1/counterparties", payload)


async def nip_transfer(
    *,
    from_sub_account_id: str,
    counterparty_id: str,
    amount: float,
    reference: str,
    reason: str,
) -> dict:
    """
    Move money from a sub-account to an external bank (NIP).
    Returns: {"transfer_id": "...", "status": "pending"}
    """
    payload = {
        "data": {
            "type": "NIPTransfer",
            "attributes": {
                "amount": amount,
                "currency": "NGN",
                "reason": reason,
                "reference": reference,
            },
            "relationships": {
                "account": {
                    "data": {"type": "SubAccount", "id": from_sub_account_id}
                },
                "destinationAccount": {
                    "data": {"type": "CounterParty", "id": counterparty_id}
                },
            },
        }
    }
    return await _request("POST", "/api/v1/transfers", payload)


async def get_transfer(transfer_id: str) -> dict:
    """Read the current status of a transfer. Used by reconciliation."""
    return await _request("GET", f"/api/v1/transfers/{transfer_id}")


def verify_webhook(payload_bytes: bytes, signature_header: str) -> bool:
    """
    Anchor signs webhooks with HMAC-SHA256 of the raw body using
    ANCHOR_WEBHOOK_SECRET. Adjust the header name once you read
    their docs.
    """
    if not ANCHOR_WEBHOOK_SECRET:
        logger.warning("ANCHOR_WEBHOOK_SECRET not set — accepting all webhooks (dev mode)")
        return True

    expected = hmac.new(
        ANCHOR_WEBHOOK_SECRET.encode(),
        payload_bytes,
        hashlib.sha256,
    ).hexdigest()

    provided = (signature_header or "").strip().lower().replace("sha256=", "")
    return hmac.compare_digest(expected, provided)