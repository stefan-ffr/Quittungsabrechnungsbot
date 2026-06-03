"""Optional integration: push the user's own receipt/settlement transactions to
a Money Manager instance (https://github.com/stefan-ffr/money-manager).

The bot stays the source of truth for the detailed person/item split; this only
mirrors the user's own cash flows into a dedicated "Quittungsabrechnung" account
in Money Manager. Pushes are idempotent via `external_ref`, so re-syncing is safe.

Mapping (user's perspective):
- receipt  -> expense  amount = -my_share        external_ref = "receipt-<id>"
- transfer received -> income   amount = +amount external_ref = "transfer-<id>"
- transfer paid     -> expense  amount = -amount external_ref = "transfer-<id>"
"""

import logging
from datetime import date, datetime
from typing import Optional

import httpx

import app.db as db
from app.config import MONEY_MANAGER_URL, MONEY_MANAGER_API_KEY, CURRENCY

log = logging.getLogger(__name__)

_ENDPOINT = "/api/v1/integrations/receipt-bot/transactions"


def enabled() -> bool:
    return bool(MONEY_MANAGER_URL and MONEY_MANAGER_API_KEY)


def _norm_date(value: Optional[str], fallback: Optional[str] = None) -> str:
    """Return a YYYY-MM-DD string from a receipt/transfer date field."""
    for candidate in (value, fallback):
        if candidate:
            text = str(candidate).strip()
            if len(text) >= 10 and text[4] == "-" and text[7] == "-":
                return text[:10]
    return date.today().isoformat()


def build_transactions(group_id: Optional[int]) -> list[dict]:
    """Collect the user's own transactions for a group as Money Manager payloads."""
    transactions: list[dict] = []

    for r in db.get_receipts(limit=1000, group_id=group_id):
        my_share = r["my_share"] or 0
        if my_share <= 0:
            continue  # nothing of this receipt is the user's own expense
        transactions.append({
            "date": _norm_date(r["date"], r["uploaded_at"]),
            "amount": -round(float(my_share), 2),
            "description": (r["store"] or "Quittung"),
            "category": "Quittung",
            "currency": r["currency"] or CURRENCY,
            "external_ref": f"receipt-{r['id']}",
        })

    for t in db.get_cash_transfers(limit=1000, group_id=group_id):
        amount = round(float(t["amount"] or 0), 2)
        signed = amount if t["direction"] == "received" else -amount
        label = t["note"] or t["name"] or "Ausgleich"
        transactions.append({
            "date": _norm_date(t["created_at"]),
            "amount": signed,
            "description": f"Ausgleich {t['name']}: {label}" if t["name"] else label,
            "category": "Ausgleich",
            "currency": CURRENCY,
            "external_ref": f"transfer-{t['id']}",
        })

    return transactions


async def push(transactions: list[dict]) -> dict:
    """Send transactions to Money Manager. Returns {created, skipped, account_id}."""
    if not enabled():
        raise RuntimeError("Money Manager Integration nicht konfiguriert")
    if not transactions:
        return {"created": 0, "skipped": 0, "account_id": None}

    url = MONEY_MANAGER_URL.rstrip("/") + _ENDPOINT
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url,
            json={"transactions": transactions},
            headers={"X-API-Key": MONEY_MANAGER_API_KEY},
        )
        resp.raise_for_status()
        return resp.json()


async def sync_group(group_id: Optional[int]) -> dict:
    """Build and push all of a group's transactions (idempotent)."""
    return await push(build_transactions(group_id))
