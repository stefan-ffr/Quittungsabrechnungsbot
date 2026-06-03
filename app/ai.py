import base64
import io
import json
import re
import anthropic
import openai
from PIL import Image
from pdf2image import convert_from_bytes
from app.config import (
    AI_PROVIDER, ANTHROPIC_API_KEY, OPENROUTER_API_KEY, AI_MODEL,
)

SYSTEM_PROMPT = """Du bist ein Spezialist für das exakte Lesen von Quittungen und Kassenbelegen.
Antworte NUR mit einem JSON-Objekt – kein Markdown, keine Erklärungen.

JSON-Schema:
{
  "store": "Name des Geschäfts oder Restaurants",
  "date": "Datum im Format YYYY-MM-DD oder leer",
  "currency": "ISO-Code: CHF, EUR, USD, THB, JPY, ...",
  "total": Gesamtbetrag als Zahl,
  "items": [
    {
      "description": "Artikelname",
      "quantity": Menge als Zahl,
      "amount": Gesamtpreis dieses Artikels als Zahl
    }
  ]
}

ÜBERSETZUNG (kritisch):
- Übersetze description + store ins Deutsche WENN du den Originaltext
  zuverlässig lesen und verstehen kannst.
- WENN UNSICHER: lieber den Original-Text (auch Thai, kyrillisch etc.) UNVERÄNDERT
  in description schreiben, statt zu erfinden oder zu raten. Niemals halluzinieren.
- WENN du eine echte Marke erkennst (z.B. CP, HL, BR, 7-Eleven, Coca-Cola),
  behalte den Markennamen + ergänze auf Deutsch was es generisch ist.
  Beispiel: "Coca-Cola Zero 500ml" oder "CP Würstchen 200g (Schweinefleisch)".
- Mengenangaben + Größen (g, ml, kg, l, %) bleiben unverändert.
- Reihenfolge der Wörter darf ans Deutsche angepasst werden.

KATEGORIE-HINWEISE (hilft Übersetzung richtig zu wählen):
- "PF50+" oder "SPF50+" = Sonnenschutzfaktor → Sonnencreme, NICHT Wassermelone!
- Liter-/ml-Größen + Schaumlogo = oft Geschirrspülmittel/Shampoo, nicht Seife
- 7-11/CP-Quittungen sind häufig Snacks/Getränke/Convenience.

WAS DU NICHT TUN SOLLST:
- Items NICHT generisch durch "Produkt 500g" ersetzen wenn du den Namen
  nicht lesen kannst — lass den Original-Text drin oder schreib "?".
- Wenn ein Item-Name unleserlich ist: description = "[unlesbar]" + Originalfragment.

BERECHNUNG:
- "amount" = Gesamtpreis dieses Artikels (qty × Einzelpreis)
- Rabatte als eigene negative items
- Trinkgeld/Service als eigene items
"""

USER_PROMPT = "Extrahiere alle Positionen aus dieser Quittung."


def _to_jpeg_bytes(img: Image.Image) -> bytes:
    """Convert a PIL Image to JPEG bytes."""
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _file_to_images(file_bytes: bytes, mime_type: str) -> list[bytes]:
    """Convert any supported file to a list of JPEG byte arrays."""
    if mime_type == "application/pdf":
        pages = convert_from_bytes(file_bytes, dpi=300)
        return [_to_jpeg_bytes(page) for page in pages]

    # Already an image – normalize to JPEG
    try:
        img = Image.open(io.BytesIO(file_bytes))
        return [_to_jpeg_bytes(img)]
    except Exception:
        # Fallback: assume it's already a valid JPEG
        return [file_bytes]


def _encode(data: bytes) -> str:
    return base64.standard_b64encode(data).decode("utf-8")


def _extract_anthropic(images: list[bytes]) -> str:
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    model = AI_MODEL or "claude-sonnet-4-20250514"

    content = []
    for img in images:
        content.append({
            "type": "image",
            "source": {"type": "base64", "media_type": "image/jpeg", "data": _encode(img)},
        })
    content.append({"type": "text", "text": USER_PROMPT})

    response = client.messages.create(
        model=model, max_tokens=2048, system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": content}],
    )
    return response.content[0].text.strip()


def _extract_openrouter(images: list[bytes]) -> str:
    client = openai.OpenAI(
        api_key=OPENROUTER_API_KEY,
        base_url="https://openrouter.ai/api/v1",
    )
    model = AI_MODEL or "anthropic/claude-sonnet-4"

    user_content = []
    for img in images:
        data_url = f"data:image/jpeg;base64,{_encode(img)}"
        user_content.append({"type": "image_url", "image_url": {"url": data_url}})
    user_content.append({"type": "text", "text": USER_PROMPT})

    response = client.chat.completions.create(
        model=model, max_tokens=2048,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ],
    )
    return response.choices[0].message.content.strip()


def extract_receipt(file_bytes: bytes, mime_type: str) -> dict:
    """Call AI to extract receipt data from image or PDF bytes."""
    images = _file_to_images(file_bytes, mime_type)

    if AI_PROVIDER == "openrouter":
        raw = _extract_openrouter(images)
    else:
        raw = _extract_anthropic(images)

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
