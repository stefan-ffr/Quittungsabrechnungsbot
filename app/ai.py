import base64
import json
import re
import anthropic
import openai
from app.config import (
    AI_PROVIDER, ANTHROPIC_API_KEY, OPENROUTER_API_KEY, AI_MODEL,
)

SYSTEM_PROMPT = """Du bist ein Spezialist für das Lesen von Quittungen und Kassenbelegen.
Extrahiere alle relevanten Informationen und antworte NUR mit einem JSON-Objekt – kein Markdown, keine Erklärungen.

JSON-Schema:
{
  "store": "Name des Geschäfts oder Restaurants",
  "date": "Datum im Format YYYY-MM-DD oder leer",
  "currency": "CHF oder EUR oder USD",
  "total": Gesamtbetrag als Zahl,
  "items": [
    {
      "description": "Artikelname",
      "quantity": Menge als Zahl,
      "amount": Gesamtpreis dieses Artikels als Zahl
    }
  ]
}

Wichtig:
- Entferne Duplikate (z.B. Rabatt-Zeilen separat aufführen)
- "amount" ist der Gesamtpreis des Artikels (qty × Einzelpreis)
- Wenn Trinkgeld/Service vorhanden: als eigenen Item aufführen
- Währung erkennen anhand des Symbols oder des Landes
"""

USER_PROMPT = "Extrahiere alle Positionen aus dieser Quittung."


def _encode_file(file_bytes: bytes) -> str:
    return base64.standard_b64encode(file_bytes).decode("utf-8")


def _extract_anthropic(file_bytes: bytes, mime_type: str) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    model = AI_MODEL or "claude-sonnet-4-20250514"

    if mime_type == "application/pdf":
        content = [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": _encode_file(file_bytes)}},
            {"type": "text", "text": USER_PROMPT},
        ]
    else:
        content = [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": _encode_file(file_bytes)}},
            {"type": "text", "text": USER_PROMPT},
        ]

    response = client.messages.create(
        model=model, max_tokens=2048, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text.strip()


def _extract_openrouter(file_bytes: bytes, mime_type: str) -> str:
    client = openai.OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )
    model = AI_MODEL or "anthropic/claude-sonnet-4"
    b64 = _encode_file(file_bytes)

    # OpenRouter/Anthropic unterstützt nur bestimmte Bildformate
    supported_image = {"image/jpeg", "image/png", "image/gif", "image/webp"}
    if mime_type not in supported_image and mime_type != "application/pdf":
        # Fallback: als JPEG behandeln (Telegram konvertiert Fotos meist zu JPEG)
        mime_type = "image/jpeg"

    data_url = f"data:{mime_type};base64,{b64}"

    response = client.chat.completions.create(
        model=model, max_tokens=2048,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": USER_PROMPT},
            ]},
        ],
    )
    return response.choices[0].message.content.strip()


def extract_receipt(file_bytes: bytes, mime_type: str) -> dict:
    """Call AI to extract receipt data from image or PDF bytes."""
    if AI_PROVIDER == "openrouter":
        raw = _extract_openrouter(file_bytes, mime_type)
    else:
        raw = _extract_anthropic(file_bytes, mime_type)

    # Strip possible markdown fences
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        data = {
            "store": "Unbekannt", "date": "", "currency": "CHF",
            "total": 0.0, "items": [], "_raw": raw,
        }

    data["total"] = float(data.get("total") or 0)
    for item in data.get("items", []):
        item["amount"] = float(item.get("amount") or 0)
        item["quantity"] = float(item.get("quantity") or 1)

    return data
