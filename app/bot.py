"""
Telegram Bot – Quittungsmanagement & Schulden/Guthaben

Reply-Keyboard:
  IDLE:   Alle Haupt-Aktionen sichtbar
  AKTIV:  Nur [❌ Abbrechen] – wenn ein Flow läuft
"""
import os
import logging
import magic
from datetime import datetime

from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    ReplyKeyboardMarkup, KeyboardButton, BotCommand,
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters,
)

from app.config import TELEGRAM_TOKEN, ALLOWED_CHAT_IDS, UPLOAD_PATH
import app.db as db
import app.ai as ai

log = logging.getLogger(__name__)
CURRENCY = "CHF"

sessions: dict[int, dict] = {}


def _auth(update: Update) -> bool:
    return not ALLOWED_CHAT_IDS or update.effective_chat.id in ALLOWED_CHAT_IDS


# ── Reply-Keyboards ───────────────────────────────────────────────────────────

KBD_IDLE = ReplyKeyboardMarkup(
    [
        [KeyboardButton("💸 Zahlung buchen"), KeyboardButton("💰 Saldo")],
        [KeyboardButton("📋 Kontoauszug"),    KeyboardButton("🧾 Quittungen")],
        [KeyboardButton("👥 Personen"),        KeyboardButton("❌ Abbrechen")],
    ],
    resize_keyboard=True,
    is_persistent=True,
)

KBD_ACTIVE = ReplyKeyboardMarkup(
    [[KeyboardButton("❌ Abbrechen")]],
    resize_keyboard=True,
    is_persistent=True,
)


async def _set_kbd(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, active: bool):
    """Stilles Keyboard-Update – sendet eine unsichtbare Nachricht mit dem neuen Keyboard."""
    kbd = KBD_ACTIVE if active else KBD_IDLE
    # Wir verwenden eine Leerzeichen-Nachricht die sofort gelöscht wird.
    # Einfacher: wir hängen das Keyboard an die nächste echte Nachricht –
    # deshalb reichen wir es durch alle reply-Methoden durch.
    # Diese Hilfsfunktion merkt sich nur den Zustand.
    ctx.chat_data["kbd_active"] = active


def _kbd(ctx: ContextTypes.DEFAULT_TYPE) -> ReplyKeyboardMarkup:
    return KBD_ACTIVE if ctx.chat_data.get("kbd_active") else KBD_IDLE


async def _reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                 text: str, inline: InlineKeyboardMarkup | None = None,
                 set_active: bool | None = None):
    """
    Sendet Antwort mit aktuellem Reply-Keyboard.
    set_active=True  → schaltet auf AKTIV-Keyboard um
    set_active=False → schaltet zurück auf IDLE-Keyboard
    set_active=None  → behält aktuellen Zustand
    """
    if set_active is not None:
        ctx.chat_data["kbd_active"] = set_active
    kbd = _kbd(ctx)

    if inline:
        await update.message.reply_text(text, reply_markup=kbd, parse_mode="Markdown")
        await update.message.reply_text("👇", reply_markup=inline)
    else:
        await update.message.reply_text(text, reply_markup=kbd, parse_mode="Markdown")


async def _send(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
                text: str, set_active: bool | None = None,
                inline: InlineKeyboardMarkup | None = None):
    """
    Sendet eine neue Nachricht (z.B. aus Callback-Handlers heraus).
    Nutzt ctx.bot.send_message damit das Reply-Keyboard aktualisiert wird.
    """
    if set_active is not None:
        ctx.chat_data["kbd_active"] = set_active
    kbd = _kbd(ctx)

    if inline:
        await ctx.bot.send_message(chat_id, text, reply_markup=kbd, parse_mode="Markdown")
        await ctx.bot.send_message(chat_id, "👇", reply_markup=inline)
    else:
        await ctx.bot.send_message(chat_id, text, reply_markup=kbd, parse_mode="Markdown")


# ── Inline-Keyboard Helpers ───────────────────────────────────────────────────

def _persons_toggle_kbd(selected: list[int], callback_prefix: str,
                        confirm_label: str = "✅ Zuweisen",
                        confirm_cb: str = "confirm",
                        extra: list | None = None) -> InlineKeyboardMarkup:
    persons = db.get_persons()
    rows = []
    for p in persons:
        tick = "✅ " if p["id"] in selected else "◻️ "
        rows.append([InlineKeyboardButton(
            f"{tick}{p['name']}", callback_data=f"{callback_prefix}:{p['id']}"
        )])
    rows.append([InlineKeyboardButton("➕ Person hinzufügen", callback_data="inline_add_person")])
    label = f"{confirm_label} ({len(selected)})" if selected else f"{confirm_label} …"
    rows.append([InlineKeyboardButton(
        label, callback_data=confirm_cb if selected else "noop"
    )])
    if extra:
        rows.extend(extra)
    rows.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def _simple_persons_kbd(callback_prefix: str,
                        extra: list | None = None) -> InlineKeyboardMarkup:
    persons = db.get_persons()
    rows = [[InlineKeyboardButton(p["name"], callback_data=f"{callback_prefix}:{p['id']}")]
            for p in persons]
    rows.append([InlineKeyboardButton("➕ Person hinzufügen", callback_data="inline_add_person")])
    if extra:
        rows.extend(extra)
    rows.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


def _fmt_balance(b: dict) -> str:
    bal = b["balance"]
    if abs(bal) < 0.01:
        return f"⚪ *{b['name']}* – quitt"
    elif bal > 0:
        return f"🔴 *{b['name']}* – schuldet mir {CURRENCY} {bal:.2f}"
    else:
        return f"🟢 *{b['name']}* – ich schulde {CURRENCY} {abs(bal):.2f}"


# ── Reply-Keyboard Trigger-Texte ──────────────────────────────────────────────

REPLY_TRIGGERS = {
    "💸 Zahlung buchen": "geld",
    "💰 Saldo":          "saldo",
    "📋 Kontoauszug":    "detail",
    "🧾 Quittungen":     "quittungen",
    "👥 Personen":       "personen",
    "❌ Abbrechen":      "abbrechen",
}


# ── Commands ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    await _reply(update, ctx,
        "👋 *Quittungs-Bot*\n\n"
        "📸 Foto oder PDF einer Quittung senden\n"
        "💸 Zahlung buchen\n"
        "💰 Saldo & Kontoauszüge\n"
        "👥 Personen verwalten\n\n"
        "Nutze die Buttons unten ⬇️",
        set_active=False,
    )


async def cmd_personen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    persons = db.get_persons()
    if not persons:
        sessions[update.effective_chat.id] = {"stage": "person_add_name"}
        await _reply(update, ctx,
            "Noch keine Personen.\n\n➕ *Name eingeben* um erste Person anzulegen:",
            set_active=True)
        return
    balances = {b["id"]: b for b in db.get_balances()}
    lines = ["👥 *Personen & Salden:*\n"]
    for p in persons:
        b = balances.get(p["id"])
        lines.append(_fmt_balance(b) if b else f"• *{p['name']}*")
    rows = []
    for p in persons:
        rows.append([
            InlineKeyboardButton(f"📋 {p['name']}", callback_data=f"detail:{p['id']}"),
            InlineKeyboardButton("🗑️", callback_data=f"person_del:{p['id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ Person hinzufügen", callback_data="person_add_start")])
    await _reply(update, ctx, "\n".join(lines),
                 inline=InlineKeyboardMarkup(rows), set_active=False)


async def cmd_person_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    if not ctx.args:
        sessions[update.effective_chat.id] = {"stage": "person_add_name"}
        await _reply(update, ctx, "➕ *Name der neuen Person eingeben:*", set_active=True)
        return
    name = " ".join(ctx.args).strip()
    db.add_person(name)
    await _reply(update, ctx, f"✅ *{name}* hinzugefügt.", set_active=False)


async def cmd_person_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    if not ctx.args:
        persons = db.get_persons()
        if not persons:
            await _reply(update, ctx, "Keine Personen vorhanden.", set_active=False)
            return
        rows = [[InlineKeyboardButton(f"🗑️ {p['name']}", callback_data=f"person_del:{p['id']}")]
                for p in persons]
        rows.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
        await _reply(update, ctx, "🗑️ *Welche Person löschen?*",
                     inline=InlineKeyboardMarkup(rows), set_active=True)
        return
    name = " ".join(ctx.args).strip()
    match = next((p for p in db.get_persons() if p["name"].lower() == name.lower()), None)
    if not match:
        await _reply(update, ctx, f"❌ '{name}' nicht gefunden.")
        return
    db.delete_person(match["id"])
    await _reply(update, ctx, f"🗑️ *{match['name']}* entfernt.", set_active=False)


async def cmd_saldo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    balances = db.get_balances()
    if not balances:
        await _reply(update, ctx, "Keine Daten.\n/person_add [Name]", set_active=False)
        return
    lines = ["💰 *Aktuelle Salden:*\n"]
    for b in balances:
        lines.append(_fmt_balance(b))
        details = []
        if b["owed_me"] > 0:  details.append(f"Quitt. von dir: +{CURRENCY} {b['owed_me']:.2f}")
        if b["i_owe"] > 0:    details.append(f"Quitt. von ihnen: −{CURRENCY} {b['i_owe']:.2f}")
        if b["received"] > 0: details.append(f"Erhalten: −{CURRENCY} {b['received']:.2f}")
        if b["paid"] > 0:     details.append(f"Gegeben: +{CURRENCY} {b['paid']:.2f}")
        if details:
            lines.append("   ↳ " + " | ".join(details))
    total_mir = sum(b["balance"] for b in balances if b["balance"] > 0)
    total_ich = sum(abs(b["balance"]) for b in balances if b["balance"] < 0)
    lines.append(f"\n📊 Offen (an mich): {CURRENCY} {total_mir:.2f}")
    if total_ich > 0:
        lines.append(f"📊 Meine Schulden:  {CURRENCY} {total_ich:.2f}")
    rows = [[InlineKeyboardButton(f"📋 {b['name']}", callback_data=f"detail:{b['id']}")]
            for b in balances]
    await _reply(update, ctx, "\n".join(lines),
                 inline=InlineKeyboardMarkup(rows) if rows else None,
                 set_active=False)


async def cmd_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    if ctx.args:
        name = " ".join(ctx.args).strip()
        p = next((x for x in db.get_persons() if x["name"].lower() == name.lower()), None)
        if not p:
            await _reply(update, ctx, f"❌ '{name}' nicht gefunden.")
            return
        await _send_detail(ctx, update.effective_chat.id, p["id"])
    else:
        sessions[update.effective_chat.id] = {"stage": "detail_select"}
        await _reply(update, ctx, "Für wen?",
                     inline=_simple_persons_kbd("detail"),
                     set_active=True)


async def _send_detail(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, person_id: int):
    hist     = db.get_person_history(person_id)
    balances = {b["id"]: b for b in db.get_balances()}
    b        = balances.get(person_id, {})
    name     = hist["person"].get("name", "?")
    lines    = [f"📋 *Kontoauszug: {name}*\n", _fmt_balance(b) if b else "", ""]

    if hist["items_owe_me"]:
        lines.append("🧾 *Du hast gezahlt – sie schulden dir:*")
        for it in hist["items_owe_me"][:12]:
            store = it.get("store") or "?"
            date  = (it.get("date") or "")[:10]
            lines.append(f"  • {it['description']} {CURRENCY} {it['share_amount']:.2f} ({store} {date})")
        if len(hist["items_owe_me"]) > 12:
            lines.append(f"  … +{len(hist['items_owe_me'])-12} weitere")

    if hist["receipts_they_paid"]:
        lines.append("\n🧾 *Sie haben gezahlt – du schuldest ihnen:*")
        for r in hist["receipts_they_paid"]:
            store = r.get("store") or "?"
            date  = (r.get("date") or "")[:10]
            lines.append(f"  • {store} {date} – dein Anteil: {CURRENCY} {r['my_share']:.2f}")

    if hist["transfers"]:
        lines.append("\n💸 *Bargeld:*")
        for t in hist["transfers"][:12]:
            arrow = "📥 von dir erhalten" if t["direction"] == "received" else "📤 an sie gegeben"
            note  = f" ({t['note']})" if t.get("note") else ""
            date  = str(t.get("created_at", ""))[:10]
            lines.append(f"  • {arrow}: {CURRENCY} {t['amount']:.2f}{note} [{date}]")

    if not hist["items_owe_me"] and not hist["receipts_they_paid"] and not hist["transfers"]:
        lines.append("_Noch keine Einträge._")

    await _send(ctx, chat_id, "\n".join(lines), set_active=False)


async def cmd_verlauf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    transfers = db.get_cash_transfers(limit=20)
    if not transfers:
        await _reply(update, ctx, "Noch keine Zahlungen.", set_active=False)
        return
    lines = ["💸 *Letzte Zahlungen:*\n"]
    for t in transfers:
        arrow = "📥 von" if t["direction"] == "received" else "📤 an"
        note  = f" · _{t['note']}_" if t.get("note") else ""
        date  = str(t.get("created_at", ""))[:10]
        lines.append(f"[{t['id']}] {arrow} *{t['name']}*: {CURRENCY} {t['amount']:.2f}{note} [{date}]")
    rows = [[InlineKeyboardButton("🗑️ Letzte Zahlung löschen", callback_data="del:last_transfer")]]
    await _reply(update, ctx, "\n".join(lines),
                 inline=InlineKeyboardMarkup(rows), set_active=False)


async def cmd_quittungen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    receipts = db.get_receipts(10)
    if not receipts:
        await _reply(update, ctx, "Noch keine Quittungen.", set_active=False)
        return
    lines = ["🧾 *Letzte Quittungen:*\n"]
    for r in receipts:
        date  = (r["date"] or str(r["uploaded_at"])[:10])
        store = r["store"] or "Unbekannt"
        total = f"{CURRENCY} {r['total']:.2f}" if r["total"] else "?"
        payer = f"gezahlt von *{r['payer_name']}*" if r["payer_name"] else "von *mir* gezahlt"
        lines.append(f"• *{store}* – {date} – {total} · {payer}")
    rows = [[InlineKeyboardButton("🗑️ Letzte Quittung löschen", callback_data="del:last_receipt")]]
    await _reply(update, ctx, "\n".join(lines),
                 inline=InlineKeyboardMarkup(rows), set_active=False)


async def cmd_geld(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    sessions[update.effective_chat.id] = {"stage": "geld_direction"}
    await _reply(update, ctx,
        "💸 *Zahlung buchen*\n\nIn welche Richtung?",
        inline=InlineKeyboardMarkup([
            [InlineKeyboardButton("📥 Person gibt mir Geld", callback_data="geld_dir:received")],
            [InlineKeyboardButton("📤 Ich gebe Person Geld", callback_data="geld_dir:paid")],
            [InlineKeyboardButton("❌ Abbrechen",            callback_data="cancel")],
        ]),
        set_active=True,
    )


async def cmd_loeschen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    await _reply(update, ctx, "Was löschen?",
        inline=InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Letzte Zahlung",  callback_data="del:last_transfer")],
            [InlineKeyboardButton("🧾 Letzte Quittung", callback_data="del:last_receipt")],
            [InlineKeyboardButton("❌ Abbrechen",       callback_data="cancel")],
        ]),
        set_active=True,
    )


async def cmd_abbrechen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    sessions.pop(update.effective_chat.id, None)
    await _reply(update, ctx, "❌ Abgebrochen.", set_active=False)


# ── Datei-Handler ─────────────────────────────────────────────────────────────

async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    chat_id = update.effective_chat.id

    # Sofort auf AKTIV-Keyboard umschalten
    ctx.chat_data["kbd_active"] = True
    msg = await update.message.reply_text("🔍 Analysiere Quittung…", reply_markup=KBD_ACTIVE)

    if update.message.photo:
        tg_file = await update.message.photo[-1].get_file()
        ext, mime = ".jpg", "image/jpeg"
    elif update.message.document:
        tg_file = await update.message.document.get_file()
        fname   = update.message.document.file_name or "file"
        ext     = os.path.splitext(fname)[1] or ".bin"
        mime    = update.message.document.mime_type or "application/octet-stream"
    else:
        await msg.edit_text("❌ Nicht unterstützter Dateityp.")
        ctx.chat_data["kbd_active"] = False
        return

    os.makedirs(UPLOAD_PATH, exist_ok=True)
    ts         = datetime.now().strftime("%Y%m%d_%H%M%S")
    local_path = os.path.join(UPLOAD_PATH, f"receipt_{ts}{ext}")
    await tg_file.download_to_drive(local_path)

    if mime in ("application/octet-stream", ""):
        try:
            mime = magic.from_file(local_path, mime=True)
        except Exception:
            pass

    with open(local_path, "rb") as f:
        file_bytes = f.read()

    try:
        data = ai.extract_receipt(file_bytes, mime)
    except Exception as e:
        log.exception("AI extraction failed")
        await msg.edit_text(f"❌ KI-Fehler: {e}")
        ctx.chat_data["kbd_active"] = False
        return

    receipt_id = db.save_receipt(
        store    = data.get("store", "Unbekannt"),
        date     = data.get("date", ""),
        total    = data.get("total", 0),
        currency = data.get("currency", CURRENCY),
        note     = "",
        file_path= local_path,
        payer_id = None,
        my_share = 0.0,
    )

    raw_items = data.get("items", [])
    if not raw_items:
        await msg.edit_text(
            f"⚠️ Keine Positionen erkannt.\n"
            f"#{receipt_id} – {data.get('store','?')} – {CURRENCY} {data.get('total',0):.2f}"
        )
        ctx.chat_data["kbd_active"] = False
        return

    item_ids = db.save_items(receipt_id, raw_items)
    items_s  = [
        {"id": iid, "description": it["description"],
         "amount": it["amount"], "quantity": it["quantity"]}
        for iid, it in zip(item_ids, raw_items)
    ]

    sessions[chat_id] = {
        "stage": "payer_select",
        "receipt_id": receipt_id,
        "items": items_s,
        "data": data,
    }

    cur   = data.get("currency", CURRENCY)
    total = data.get("total") or sum(i["amount"] for i in items_s)
    lines = []
    for i, it in enumerate(items_s, 1):
        qty = f"×{int(it['quantity'])} " if it["quantity"] != 1 else ""
        lines.append(f"  {i}. {it['description']} {qty}→ {cur} {it['amount']:.2f}")

    text = (
        f"🧾 *{data.get('store','?')}*"
        + (f" – {data['date']}" if data.get("date") else "")
        + f"\n\n*Positionen:*\n" + "\n".join(lines)
        + f"\n\n💰 *Total: {cur} {total:.2f}*"
        + "\n\n❓ *Wer hat gezahlt?*"
    )
    persons = db.get_persons()
    kbd = [[InlineKeyboardButton("👤 Ich habe gezahlt", callback_data="payer:me")]]
    for p in persons:
        kbd.append([InlineKeyboardButton(f"💳 {p['name']} hat gezahlt",
                                         callback_data=f"payer:{p['id']}")])
    kbd.append([InlineKeyboardButton("➕ Person hinzufügen", callback_data="inline_add_person")])
    kbd.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
    await msg.edit_text(text, reply_markup=InlineKeyboardMarkup(kbd), parse_mode="Markdown")


# ── Zuweisung Keyboards ───────────────────────────────────────────────────────

def _assign_start_kbd(payer_is_me: bool) -> InlineKeyboardMarkup:
    persons = db.get_persons()
    rows = [[InlineKeyboardButton(
        "👥 Personen wählen (gleich aufteilen)", callback_data="assign_pick_all"
    )]]
    for p in persons:
        label = f"Alles → {p['name']}" if payer_is_me else f"Alles konsumiert: {p['name']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"assign_one:{p['id']}")])
    rows.append([InlineKeyboardButton("➕ Person hinzufügen", callback_data="inline_add_person")])
    rows.append([InlineKeyboardButton("🔀 Manuell (Position für Position)",
                                      callback_data="assign_manual")])
    rows.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


# ── Callback-Handler ──────────────────────────────────────────────────────────

async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data    = query.data
    session = sessions.get(chat_id, {})

    if data == "noop":
        return

    # ── Inline Person hinzufügen
    if data == "inline_add_person":
        session["_pre_add_stage"] = session.get("stage", "")
        session["stage"] = "inline_add_person"
        sessions[chat_id] = session
        await query.edit_message_text("➕ *Neue Person anlegen*\n\nName eingeben:",
                                      parse_mode="Markdown")
        return

    if data == "cancel":
        sessions.pop(chat_id, None)
        await query.edit_message_text("❌ Abgebrochen.")
        await _send(ctx, chat_id, "Bereit.", set_active=False)
        return

    # ── Person hinzufügen (aus Personen-Menü)
    if data == "person_add_start":
        session["stage"] = "person_add_name"
        sessions[chat_id] = session
        await query.edit_message_text("➕ *Name der neuen Person eingeben:*",
                                      parse_mode="Markdown")
        return

    # ── Person löschen
    if data.startswith("person_del:"):
        pid = int(data.split(":")[1])
        p = db.get_person(pid)
        pname = p["name"] if p else "?"
        db.delete_person(pid)
        sessions.pop(chat_id, None)
        await query.edit_message_text(f"🗑️ *{pname}* entfernt.", parse_mode="Markdown")
        await _send(ctx, chat_id, "Erledigt.", set_active=False)
        return

    # ── Detail-Auswahl
    if data.startswith("detail:"):
        pid = int(data.split(":")[1])
        sessions.pop(chat_id, None)
        await query.edit_message_text("📋 Lade…")
        await _send_detail(ctx, chat_id, pid)
        return

    # ── ZAHLER-AUSWAHL
    if data.startswith("payer:"):
        who   = data.split(":")[1]
        items = session.get("items", [])
        cur   = session.get("data", {}).get("currency", CURRENCY)
        total = session.get("data", {}).get("total") or sum(i["amount"] for i in items)
        p_map = {p["id"]: p["name"] for p in db.get_persons()}

        if who == "me":
            db.update_receipt_payer(session["receipt_id"], None, 0.0)
            session.update({"payer_id": None, "payer_name": "ich", "stage": "assign_method"})
            sessions[chat_id] = session
            await query.edit_message_text(
                f"✅ Du hast gezahlt ({cur} {total:.2f}).\n\n*Wem werden die Kosten zugewiesen?*",
                reply_markup=_assign_start_kbd(payer_is_me=True),
                parse_mode="Markdown"
            )
        else:
            pid = int(who)
            pname = p_map.get(pid, "?")
            session.update({"payer_id": pid, "payer_name": pname, "stage": "my_share_input"})
            sessions[chat_id] = session
            db.update_receipt_payer(session["receipt_id"], pid, 0.0)
            await query.edit_message_text(
                f"💳 *{pname}* hat {cur} {total:.2f} gezahlt.\n\n"
                f"Wie viel ist *dein Anteil*?\n"
                f"_(Betrag eingeben, z.B. `{total/2:.2f}` – oder `0`)_",
                parse_mode="Markdown"
            )
        return

    # ── ZUWEISUNG: eine Person
    if data.startswith("assign_one:"):
        pid   = int(data.split(":")[1])
        items = session.get("items", [])
        for item in items:
            db.assign_item(item["id"], pid, item["amount"])
        await _finish_assignment(query, ctx, chat_id, session, items)
        return

    # ── ZUWEISUNG: Personen-Picker (alle Items gleichzeitig)
    if data == "assign_pick_all":
        session.update({"stage": "pick_all", "selected_all": []})
        sessions[chat_id] = session
        items = session.get("items", [])
        total = sum(i["amount"] for i in items)
        cur   = session.get("data", {}).get("currency", CURRENCY)
        await query.edit_message_text(
            f"👥 *Wer teilt sich die Kosten?*\n"
            f"Total: {cur} {total:.2f} – wird gleich aufgeteilt\n\n"
            f"_(Mehrere wählbar)_",
            reply_markup=_persons_toggle_kbd(
                [], "pick_all_toggle",
                confirm_label="✅ Aufteilen",
                confirm_cb="pick_all_confirm"
            ),
            parse_mode="Markdown"
        )
        return

    if data.startswith("pick_all_toggle:"):
        pid      = int(data.split(":")[1])
        selected = session.get("selected_all", [])
        if pid in selected: selected.remove(pid)
        else: selected.append(pid)
        session["selected_all"] = selected
        sessions[chat_id] = session
        items = session.get("items", [])
        total = sum(i["amount"] for i in items)
        cur   = session.get("data", {}).get("currency", CURRENCY)
        n     = len(selected)
        each  = total / n if n else 0
        share_info = f"\n→ {cur} {each:.2f} je Person" if n else ""
        await query.edit_message_reply_markup(
            reply_markup=_persons_toggle_kbd(
                selected, "pick_all_toggle",
                confirm_label=f"✅ Aufteilen{share_info}",
                confirm_cb="pick_all_confirm"
            )
        )
        return

    if data == "pick_all_confirm":
        selected = session.get("selected_all", [])
        if not selected:
            await query.answer("Mindestens eine Person auswählen!", show_alert=True)
            return
        items = session.get("items", [])
        for item in items:
            db.assign_item_split(item["id"], selected, item["amount"])
        await _finish_assignment(query, ctx, chat_id, session, items)
        return

    # ── ZUWEISUNG: Manuell
    if data == "assign_manual":
        session.update({"stage": "manual", "manual_idx": 0, "selected_item": []})
        sessions[chat_id] = session
        await _show_manual_item(query, session)
        return

    if data.startswith("itm_toggle:"):
        pid      = int(data.split(":")[1])
        selected = session.get("selected_item", [])
        if pid in selected: selected.remove(pid)
        else: selected.append(pid)
        session["selected_item"] = selected
        sessions[chat_id] = session
        items = session.get("items", [])
        idx   = session.get("manual_idx", 0)
        item  = items[idx]
        n     = len(selected)
        each  = item["amount"] / n if n else 0
        cur   = session.get("data", {}).get("currency", CURRENCY)
        share_info = f"\n→ {cur} {each:.2f} je" if n else ""
        await query.edit_message_reply_markup(
            reply_markup=_persons_toggle_kbd(
                selected, "itm_toggle",
                confirm_label=f"✅ Zuweisen{share_info}",
                confirm_cb="itm_confirm",
                extra=[[InlineKeyboardButton("⏭ Überspringen", callback_data="skip_item")]]
            )
        )
        return

    if data == "itm_confirm":
        selected = session.get("selected_item", [])
        if not selected:
            await query.answer("Mindestens eine Person auswählen!", show_alert=True)
            return
        items = session.get("items", [])
        idx   = session.get("manual_idx", 0)
        db.assign_item_split(items[idx]["id"], selected, items[idx]["amount"])
        session["manual_idx"]    += 1
        session["selected_item"]  = []
        sessions[chat_id] = session
        if session["manual_idx"] >= len(items):
            await _finish_assignment(query, ctx, chat_id, session, items)
        else:
            await _show_manual_item(query, session)
        return

    if data == "skip_item":
        session["manual_idx"]   = session.get("manual_idx", 0) + 1
        session["selected_item"] = []
        sessions[chat_id] = session
        items = session.get("items", [])
        if session["manual_idx"] >= len(items):
            await _finish_assignment(query, ctx, chat_id, session, items)
        else:
            await _show_manual_item(query, session)
        return

    # ── GELD: Richtung
    if data.startswith("geld_dir:"):
        direction = data.split(":")[1]
        session.update({"direction": direction, "stage": "geld_person"})
        sessions[chat_id] = session
        label = "gibt dir Geld" if direction == "received" else "bekommt Geld"
        await query.edit_message_text(
            f"💸 Wer {label}?",
            reply_markup=_simple_persons_kbd("geld_person")
        )
        return

    if data.startswith("geld_person:"):
        pid = int(data.split(":")[1])
        session.update({"person_id": pid, "stage": "geld_amount"})
        sessions[chat_id] = session
        p     = db.get_person(pid)
        pname = p["name"] if p else "?"
        d     = session.get("direction", "received")
        prompt = (
            f"💰 Wie viel hat *{pname}* dir gegeben?\n_(z.B. `45.50`)_"
            if d == "received" else
            f"💰 Wie viel gibst du *{pname}*?\n_(z.B. `45.50`)_"
        )
        await query.edit_message_text(prompt, parse_mode="Markdown")
        return

    # ── LÖSCHEN
    if data == "del:last_transfer":
        transfers = db.get_cash_transfers(limit=1)
        sessions.pop(chat_id, None)
        if transfers:
            t = transfers[0]
            db.delete_cash_transfer(t["id"])
            await query.edit_message_text(
                f"🗑️ Zahlung gelöscht: *{t['name']}* {CURRENCY} {t['amount']:.2f}",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Keine Zahlungen vorhanden.")
        await _send(ctx, chat_id, "Erledigt.", set_active=False)
        return

    if data == "del:last_receipt":
        receipts = db.get_receipts(1)
        sessions.pop(chat_id, None)
        if receipts:
            r = receipts[0]
            db.delete_receipt(r["id"])
            await query.edit_message_text(
                f"🗑️ Quittung gelöscht: *{r['store'] or 'Quittung'}* {CURRENCY} {r['total']:.2f}",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Keine Quittungen vorhanden.")
        await _send(ctx, chat_id, "Erledigt.", set_active=False)
        return

    # ── Notiz überspringen
    if data == "note_skip":
        stage = session.get("stage")
        if stage == "geld_note":
            await _save_geld(ctx, chat_id, session, note="")
        elif stage == "my_share_note":
            await _show_assign_after_share(ctx, chat_id, session)
        return


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

async def _rebuild_person_keyboard(ctx: ContextTypes.DEFAULT_TYPE,
                                   chat_id: int, session: dict, stage: str):
    """Nach Inline-Person-Erstellung das passende Keyboard neu senden."""
    items = session.get("items", [])
    cur = session.get("data", {}).get("currency", CURRENCY) if session.get("data") else CURRENCY

    if stage == "payer_select":
        total = session.get("data", {}).get("total") or sum(i["amount"] for i in items)
        persons = db.get_persons()
        kbd = [[InlineKeyboardButton("👤 Ich habe gezahlt", callback_data="payer:me")]]
        for p in persons:
            kbd.append([InlineKeyboardButton(f"💳 {p['name']} hat gezahlt",
                                             callback_data=f"payer:{p['id']}")])
        kbd.append([InlineKeyboardButton("➕ Person hinzufügen", callback_data="inline_add_person")])
        kbd.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
        await _send(ctx, chat_id, "❓ *Wer hat gezahlt?*",
                    inline=InlineKeyboardMarkup(kbd))

    elif stage == "assign_method":
        payer_is_me = session.get("payer_id") is None
        await _send(ctx, chat_id, "*Wem werden die Kosten zugewiesen?*",
                    inline=_assign_start_kbd(payer_is_me=payer_is_me))

    elif stage == "pick_all":
        selected = session.get("selected_all", [])
        total = sum(i["amount"] for i in items)
        n = len(selected)
        each = total / n if n else 0
        share_info = f"\n→ {cur} {each:.2f} je Person" if n else ""
        await _send(ctx, chat_id,
            f"👥 *Wer teilt sich die Kosten?*\nTotal: {cur} {total:.2f}\n_(Mehrere wählbar)_",
            inline=_persons_toggle_kbd(
                selected, "pick_all_toggle",
                confirm_label=f"✅ Aufteilen{share_info}",
                confirm_cb="pick_all_confirm"
            ))

    elif stage == "manual":
        selected = session.get("selected_item", [])
        idx = session.get("manual_idx", 0)
        if idx < len(items):
            item = items[idx]
            payer_is_me = session.get("payer_id") is None
            qty_txt = f"×{int(item['quantity'])} " if item["quantity"] != 1 else ""
            hint = "Wem wird zugewiesen?" if payer_is_me else f"Wer hat's konsumiert?"
            n = len(selected)
            each = item["amount"] / n if n else 0
            share_info = f"\n→ {cur} {each:.2f} je" if n else ""
            text = (
                f"📦 *Position {idx+1}/{len(items)}*\n\n"
                f"*{item['description']}* {qty_txt}\n"
                f"💶 {cur} {item['amount']:.2f}\n\n_{hint}_"
            )
            await _send(ctx, chat_id, text,
                        inline=_persons_toggle_kbd(
                            selected, "itm_toggle",
                            confirm_label=f"✅ Zuweisen{share_info}",
                            confirm_cb="itm_confirm",
                            extra=[[InlineKeyboardButton("⏭ Überspringen", callback_data="skip_item")]]
                        ))

    elif stage == "geld_person":
        d = session.get("direction", "received")
        label = "gibt dir Geld" if d == "received" else "bekommt Geld"
        await _send(ctx, chat_id, f"💸 Wer {label}?",
                    inline=_simple_persons_kbd("geld_person"))

    elif stage == "detail_select":
        await _send(ctx, chat_id, "Für wen?",
                    inline=_simple_persons_kbd("detail"))


async def _show_manual_item(query, session: dict):
    items    = session.get("items", [])
    idx      = session.get("manual_idx", 0)
    item     = items[idx]
    selected = session.get("selected_item", [])
    cur      = session.get("data", {}).get("currency", CURRENCY)
    payer_is_me = session.get("payer_id") is None
    qty_txt  = f"×{int(item['quantity'])} " if item["quantity"] != 1 else ""
    hint = "Wem wird zugewiesen?" if payer_is_me else f"Wer hat's konsumiert? (schuldet {session.get('payer_name','?')})"
    n    = len(selected)
    each = item["amount"] / n if n else 0
    share_info = f"\n→ {cur} {each:.2f} je" if n else ""

    text = (
        f"📦 *Position {idx+1}/{len(items)}*\n\n"
        f"*{item['description']}* {qty_txt}\n"
        f"💶 {cur} {item['amount']:.2f}\n\n"
        f"_{hint}_\n"
        f"_(Mehrere Personen wählbar)_"
    )
    await query.edit_message_text(
        text,
        reply_markup=_persons_toggle_kbd(
            selected, "itm_toggle",
            confirm_label=f"✅ Zuweisen{share_info}",
            confirm_cb="itm_confirm",
            extra=[[InlineKeyboardButton("⏭ Überspringen", callback_data="skip_item")]]
        ),
        parse_mode="Markdown"
    )


async def _finish_assignment(query, ctx: ContextTypes.DEFAULT_TYPE,
                              chat_id: int, session: dict, items: list):
    sessions.pop(chat_id, None)
    payer_name  = session.get("payer_name", "ich")
    cur         = session.get("data", {}).get("currency", CURRENCY) if session.get("data") else CURRENCY
    total       = sum(i["amount"] for i in items)
    my_share    = session.get("my_share", 0.0)
    payer_is_me = session.get("payer_id") is None

    if payer_is_me:
        msg = (f"✅ *{len(items)} Positionen* ({cur} {total:.2f}) zugewiesen.\n"
               f"💳 Bezahlt von: *dir*")
    else:
        msg = (f"✅ *{len(items)} Positionen* zugewiesen.\n"
               f"💳 Bezahlt von: *{payer_name}*\n"
               + (f"📌 Dein Anteil: {cur} {my_share:.2f} gebucht" if my_share > 0 else ""))

    await query.edit_message_text(msg, parse_mode="Markdown")
    await _send(ctx, chat_id, "Gespeichert. /saldo für Übersicht.", set_active=False)


async def _show_assign_after_share(ctx: ContextTypes.DEFAULT_TYPE,
                                   chat_id: int, session: dict):
    items    = session.get("items", [])
    pname    = session.get("payer_name", "?")
    cur      = session.get("data", {}).get("currency", CURRENCY)
    total    = session.get("data", {}).get("total") or sum(i["amount"] for i in items)
    my_share = session.get("my_share", 0.0)
    others   = round(total - my_share, 2)
    session["stage"] = "assign_method"
    sessions[chat_id] = session

    text = (
        f"📌 Dein Anteil: *{cur} {my_share:.2f}* bei *{pname}* gebucht.\n\n"
        + (f"Noch *{cur} {others:.2f}* aufteilen:\n" if others > 0.01 else "")
        + "\n*Wer hat was konsumiert?*"
    )
    await _send(ctx, chat_id, text,
                inline=_assign_start_kbd(payer_is_me=False))


async def _save_geld(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
                     session: dict, note: str = ""):
    person_id = session["person_id"]
    amount    = session["amount"]
    direction = session["direction"]
    sessions.pop(chat_id, None)
    db.add_cash_transfer(person_id, amount, direction, note)
    p     = db.get_person(person_id)
    pname = p["name"] if p else "?"
    note_txt = f"\n_Notiz: {note}_" if note else ""
    msg = (
        f"✅ *{pname}* hat dir *{CURRENCY} {amount:.2f}* gegeben.{note_txt}"
        if direction == "received" else
        f"✅ Du hast *{pname}* *{CURRENCY} {amount:.2f}* gegeben.{note_txt}"
    )
    await _send(ctx, chat_id, msg + "\n\n/saldo", set_active=False)


# ── Text-Eingaben ─────────────────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    chat_id = update.effective_chat.id
    text    = update.message.text.strip()

    # Reply-Keyboard Buttons
    if text in REPLY_TRIGGERS:
        cmd = REPLY_TRIGGERS[text]
        ctx.args = []
        handlers = {
            "geld":       cmd_geld,
            "saldo":      cmd_saldo,
            "detail":     cmd_detail,
            "quittungen": cmd_quittungen,
            "personen":   cmd_personen,
            "abbrechen":  cmd_abbrechen,
        }
        if cmd in handlers:
            await handlers[cmd](update, ctx)
        return

    session = sessions.get(chat_id, {})
    raw     = text.replace(",", ".")

    # ── Person hinzufügen (standalone)
    if session.get("stage") == "person_add_name":
        name = text.strip()
        if not name:
            await _reply(update, ctx, "❌ Name darf nicht leer sein.")
            return
        db.add_person(name)
        sessions.pop(chat_id, None)
        await _reply(update, ctx, f"✅ *{name}* hinzugefügt.", set_active=False)
        return

    # ── Inline Person erstellen
    if session.get("stage") == "inline_add_person":
        name = text.strip()
        if not name:
            await _reply(update, ctx, "❌ Name darf nicht leer sein.")
            return
        db.add_person(name)
        prev_stage = session.get("_pre_add_stage", "")
        session.pop("_pre_add_stage", None)
        session["stage"] = prev_stage
        sessions[chat_id] = session
        await _reply(update, ctx, f"✅ *{name}* hinzugefügt.")
        await _rebuild_person_keyboard(ctx, chat_id, session, prev_stage)
        return

    if session.get("stage") == "my_share_input":
        try:
            my_share = float(raw)
            if my_share < 0: raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Bitte Zahl eingeben (z.B. `25.00`):",
                                            parse_mode="Markdown")
            return
        session.update({"my_share": my_share, "stage": "my_share_note"})
        sessions[chat_id] = session
        db.update_receipt_payer(session["receipt_id"], session["payer_id"], my_share)
        await update.message.reply_text(
            f"📌 Dein Anteil: *{CURRENCY} {my_share:.2f}* – gebucht.\n\nNotiz? _(optional)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏩ Ohne Notiz", callback_data="note_skip")]
            ])
        )
        return

    if session.get("stage") == "my_share_note":
        await _show_assign_after_share(ctx, chat_id, session)
        return

    if session.get("stage") == "geld_amount":
        try:
            amount = float(raw)
            if amount <= 0: raise ValueError
        except ValueError:
            await update.message.reply_text("❌ Bitte Zahl eingeben (z.B. `45.50`):",
                                            parse_mode="Markdown")
            return
        session.update({"amount": amount, "stage": "geld_note"})
        sessions[chat_id] = session
        await update.message.reply_text(
            "📝 Notiz? _(optional)_",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⏩ Ohne Notiz", callback_data="note_skip")]
            ])
        )
        return

    if session.get("stage") == "geld_note":
        await _save_geld(ctx, chat_id, session, note=text)
        return

    await _reply(update, ctx,
        "Schick mir eine Quittung (Foto/PDF) oder nutze die Buttons unten.")


# ── Application ───────────────────────────────────────────────────────────────

def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("personen",   cmd_personen))
    app.add_handler(CommandHandler("person_add", cmd_person_add))
    app.add_handler(CommandHandler("person_del", cmd_person_del))
    app.add_handler(CommandHandler("saldo",      cmd_saldo))
    app.add_handler(CommandHandler("detail",     cmd_detail))
    app.add_handler(CommandHandler("verlauf",    cmd_verlauf))
    app.add_handler(CommandHandler("quittungen", cmd_quittungen))
    app.add_handler(CommandHandler("geld",       cmd_geld))
    app.add_handler(CommandHandler("loeschen",   cmd_loeschen))
    app.add_handler(CommandHandler("abbrechen",  cmd_abbrechen))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    return app


async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",      "Hilfe & Übersicht"),
        BotCommand("geld",       "💸 Zahlung buchen"),
        BotCommand("saldo",      "Aktuelle Salden"),
        BotCommand("detail",     "Kontoauszug einer Person"),
        BotCommand("verlauf",    "Letzte Zahlungen"),
        BotCommand("quittungen", "Letzte Quittungen"),
        BotCommand("personen",   "Alle Personen & Salden"),
        BotCommand("person_add", "Person hinzufügen"),
        BotCommand("person_del", "Person entfernen"),
        BotCommand("loeschen",   "Letzten Eintrag rückgängig"),
        BotCommand("abbrechen",  "Eingabe abbrechen"),
    ])
