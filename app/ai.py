import base64
import json
import re
import anthropic
from app.config import ANTHROPIC_API_KEY

_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

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


def _encode_file(file_bytes: bytes) -> str:
    return base64.standard_b64encode(file_bytes).decode("utf-8")


def extract_receipt(file_bytes: bytes, mime_type: str) -> dict:
    """Call Claude to extract receipt data from image or PDF bytes."""

    if mime_type == "application/pdf":
        content = [
            {
                "type": "document",
                "source": {
                    "type": "base64",
                    "media_type": "application/pdf",
                    "data": _encode_file(file_bytes),
                },
            },
            {"type": "text", "text": "Extrahiere alle Positionen aus dieser Quittung."},
        ]
    else:
        # image/jpeg, image/png, image/webp, image/gif
        content = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": mime_type,
                    "data": _encode_file(file_bytes),
                },
            },
            {"type": "text", "text": "Extrahiere alle Positionen aus dieser Quittung."},
        ]

    response = _client.messages.create(
        model="claude-opus-4-5",
        max_tokens=2048,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )

    raw = response.content[0].text.strip()

    # Strip possible markdown fences
    raw = re.sub(r"^```(?:json)?", "", raw).strip()
    raw = re.sub(r"```$", "", raw).strip()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        # Fallback: return minimal structure
        data = {
            "store": "Unbekannt",
            "date": "",
            "currency": "CHF",
            "total": 0.0,
            "items": [],
            "_raw": raw,
        }

    # Ensure numeric types
    data["total"] = float(data.get("total") or 0)
    for item in data.get("items", []):
        item["amount"] = float(item.get("amount") or 0)
        item["quantity"] = float(item.get("quantity") or 1)

    return data
