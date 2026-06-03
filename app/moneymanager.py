"""Optional per-user integration: push the user's own receipt/settlement
transactions to a Money Manager instance.

Konfig pro chat_id (über Telegram-Bot eingerichtet) — Fallback auf globale
.env-Variablen MONEY_MANAGER_URL/MONEY_MANAGER_API_KEY für Single-User-Setups.
"""

import logging
from datetime import date
from typing import Optional

import httpx

import app.db as db
from app.config import MONEY_MANAGER_URL, MONEY_MANAGER_API_KEY, CURRENCY

log = logging.getLogger(__name__)

_ENDPOINT = "/api/v1/integrations/receipt-bot/transactions"


def _resolve_config(chat_id: Optional[int]) -> Optional[tuple[str, str]]:
    """Return (url, api_key) — per-user wenn vorhanden, sonst .env-Fallback."""
    if chat_id is not None:
        cfg = db.get_money_manager(chat_id)
        if cfg:
            return cfg["url"], cfg["api_key"]
    if MONEY_MANAGER_URL and MONEY_MANAGER_API_KEY:
        return MONEY_MANAGER_URL.rstrip("/"), MONEY_MANAGER_API_KEY
    return None


def enabled(chat_id: Optional[int] = None) -> bool:
    return _resolve_config(chat_id) is not None


def _norm_date(value: Optional[str], fallback: Optional[str] = None) -> str:
    for candidate in (value, fallback):
        if candidate:
            text = str(candidate).strip()
            if len(text) >= 10 and text[4] == "-" and text[7] == "-":
                return text[:10]
    return date.today().isoformat()


def build_transactions(group_id: Optional[int]) -> list[dict]:
    transactions: list[dict] = []
    for r in db.get_receipts(limit=1000, group_id=group_id):
        my_share = r["my_share"] or 0
        if my_share <= 0:
            continue
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


async def push(transactions: list[dict], chat_id: Optional[int]) -> dict:
    cfg = _resolve_config(chat_id)
    if not cfg:
        raise RuntimeError("Money Manager Integration nicht konfiguriert")
    url_base, api_key = cfg
    if not transactions:
        return {"created": 0, "skipped": 0, "account_id": None}
    url = url_base + _ENDPOINT
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            url, json={"transactions": transactions}, headers={"X-API-Key": api_key})
        resp.raise_for_status()
        return resp.json()


async def sync_group(group_id: Optional[int], chat_id: Optional[int]) -> dict:
    return await push(build_transactions(group_id), chat_id)


async def test_connection(chat_id: int) -> tuple[bool, str]:
    """Pingt die Money-Manager-Instanz mit einer leeren Transaction-Liste."""
    cfg = _resolve_config(chat_id)
    if not cfg:
        return False, "nicht konfiguriert"
    url_base, api_key = cfg
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                url_base + _ENDPOINT,
                json={"transactions": []},
                headers={"X-API-Key": api_key})
        if resp.status_code == 200:
            return True, "OK"
        return False, f"HTTP {resp.status_code}: {resp.text[:150]}"
    except Exception as e:
        return False, str(e)
