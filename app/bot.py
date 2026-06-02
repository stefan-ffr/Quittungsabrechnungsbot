"""
Telegram Bot – Quittungsmanagement & Schulden/Guthaben

Reply-Keyboard:
  IDLE:   Alle Haupt-Aktionen sichtbar
  AKTIV:  Nur [Abbrechen] – wenn ein Flow laeuft
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
_pending_requests: set[int] = set()  # Chat-IDs die bereits angefragt haben


def _auth(update: Update) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    chat_id = update.effective_chat.id
    return chat_id in ALLOWED_CHAT_IDS or chat_id in db.get_allowed_chats()


def _is_admin(chat_id: int) -> bool:
    return chat_id in ALLOWED_CHAT_IDS


def _active_group(chat_id: int) -> int | None:
    return db.get_active_group(chat_id)


def _my_person_id(chat_id: int, group_id: int | None = None) -> int | None:
    """Gibt die person_id zurueck wenn der Chat-User mit einer Person verknuepft ist."""
    if group_id is not None:
        p = db.get_person_by_chat(chat_id, group_id)
    else:
        p = db.get_person_by_chat(chat_id)
    return p["id"] if p else None


def _group_name(group_id: int | None) -> str:
    if group_id is None:
        return ""
    g = db.get_group(group_id)
    return g["name"] if g else ""


# ── Reply-Keyboards ───────────────────────────────────────────────────────────

_KBD_IDLE_ROWS = [
    [KeyboardButton("💸 Zahlung buchen"), KeyboardButton("➕ Posten")],
    [KeyboardButton("💰 Saldo"),          KeyboardButton("🧾 Quittungen")],
    [KeyboardButton("📋 Kontoauszug"),    KeyboardButton("📜 Verlauf")],
    [KeyboardButton("👥 Personen"),        KeyboardButton("📂 Gruppe")],
    [KeyboardButton("📁 Projekte"),        KeyboardButton("↩️ Rückgängig")],
]

KBD_IDLE = ReplyKeyboardMarkup(
    _KBD_IDLE_ROWS,
    resize_keyboard=True,
    is_persistent=True,
)

KBD_IDLE_ADMIN = ReplyKeyboardMarkup(
    _KBD_IDLE_ROWS + [[KeyboardButton("👑 Users")]],
    resize_keyboard=True,
    is_persistent=True,
)

KBD_ACTIVE = ReplyKeyboardMarkup(
    [[KeyboardButton("❌ Abbrechen")]],
    resize_keyboard=True,
    is_persistent=True,
)


async def _set_kbd(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, active: bool):
    ctx.chat_data["kbd_active"] = active


def _kbd(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int | None = None) -> ReplyKeyboardMarkup:
    if ctx.chat_data.get("kbd_active"):
        return KBD_ACTIVE
    if chat_id is not None and _is_admin(chat_id):
        return KBD_IDLE_ADMIN
    return KBD_IDLE


async def _reply(update: Update, ctx: ContextTypes.DEFAULT_TYPE,
                 text: str, inline: InlineKeyboardMarkup | None = None,
                 set_active: bool | None = None):
    if set_active is not None:
        ctx.chat_data["kbd_active"] = set_active
    kbd = _kbd(ctx, update.effective_chat.id if update and update.effective_chat else None)

    if inline:
        await update.message.reply_text(text, reply_markup=kbd, parse_mode="Markdown")
        await update.message.reply_text("👇", reply_markup=inline)
    else:
        await update.message.reply_text(text, reply_markup=kbd, parse_mode="Markdown")


async def _send(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
                text: str, set_active: bool | None = None,
                inline: InlineKeyboardMarkup | None = None):
    if set_active is not None:
        ctx.chat_data["kbd_active"] = set_active
    kbd = _kbd(ctx, chat_id)

    if inline:
        await ctx.bot.send_message(chat_id, text, reply_markup=kbd, parse_mode="Markdown")
        await ctx.bot.send_message(chat_id, "👇", reply_markup=inline)
    else:
        await ctx.bot.send_message(chat_id, text, reply_markup=kbd, parse_mode="Markdown")


# ── Inline-Keyboard Helpers ───────────────────────────────────────────────────

def _persons_toggle_kbd(selected: list[int], callback_prefix: str,
                        confirm_label: str = "✅ Zuweisen",
                        confirm_cb: str = "confirm",
                        extra: list | None = None,
                        group_id: int | None = None) -> InlineKeyboardMarkup:
    persons = db.get_persons(group_id)
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
                        extra: list | None = None,
                        group_id: int | None = None) -> InlineKeyboardMarkup:
    persons = db.get_persons(group_id)
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



def _log_action(name: str):
    """Decorator: loggt Handler-Aufrufe mit chat_id + relevantem Kontext."""
    import functools
    def deco(fn):
        @functools.wraps(fn)
        async def wrap(update, ctx, *a, **k):
            try:
                cid = update.effective_chat.id if update and update.effective_chat else "?"
                uid = update.effective_user.id if update and update.effective_user else "?"
                msg_txt = ""
                if update and update.message and update.message.text:
                    msg_txt = update.message.text[:80]
                cb_data = ""
                if update and update.callback_query:
                    cb_data = update.callback_query.data
                log.info("HANDLER %s chat=%s user=%s text=%r cb=%r",
                         name, cid, uid, msg_txt, cb_data)
            except Exception as e:
                log.warning("log_action failed: %s", e)
            return await fn(update, ctx, *a, **k)
        return wrap
    return deco


# ── Reply-Keyboard Trigger-Texte ──────────────────────────────────────────────

REPLY_TRIGGERS = {
    "💸 Zahlung buchen": "geld",
    "➕ Posten":         "posten",
    "💰 Saldo":          "saldo",
    "📋 Kontoauszug":    "detail",
    "📜 Verlauf":        "verlauf",
    "🧾 Quittungen":     "quittungen",
    "👥 Personen":       "personen",
    "📂 Gruppe":         "gruppe",
    "📁 Projekte":       "projekte",
    "↩️ Rückgängig":     "loeschen",
    "👑 Users":          "users",
    "❌ Abbrechen":      "abbrechen",
}


# ── Group check helper ───────────────────────────────────────────────────────

async def _ensure_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int | None:
    """
    Ensure user has an active group. Returns group_id or None.
    If None, the user has been prompted to create/join a group.
    """
    chat_id = update.effective_chat.id
    gid = _active_group(chat_id)
    if gid is not None:
        return gid

    # Auto-migration for existing users
    migrated = db.ensure_user_has_group(chat_id, default_name=(update.effective_user.first_name if update and update.effective_user else None))
    if migrated:
        return migrated

    # No group at all - prompt
    await _reply(update, ctx,
        "📂 *Keine Gruppe aktiv*\n\n"
        "Erstelle eine neue Gruppe oder tritt einer bei:",
        inline=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Neue Gruppe", callback_data="group_create")],
            [InlineKeyboardButton("🔗 Gruppe beitreten", callback_data="group_join")],
        ]),
        set_active=False)
    return None


async def _ensure_group_cb(query, ctx: ContextTypes.DEFAULT_TYPE, chat_id: int) -> int | None:
    """Same as _ensure_group but for callback queries."""
    gid = _active_group(chat_id)
    if gid is not None:
        return gid
    migrated = db.ensure_user_has_group(chat_id, default_name=(update.effective_user.first_name if update and update.effective_user else None))
    if migrated:
        return migrated
    await query.edit_message_text(
        "📂 *Keine Gruppe aktiv*\n\n"
        "Nutze /gruppe um eine Gruppe zu erstellen oder beizutreten.",
        parse_mode="Markdown")
    return None


# ── Commands ──────────────────────────────────────────────────────────────────

@_log_action("cmd_start")
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id

    # Deep-link: /start join_INVITECODE_PERSONID
    if ctx.args and ctx.args[0].startswith("join_"):
        parts = ctx.args[0].split("_", 2)
        if len(parts) == 3:
            invite_code, person_id_str = parts[1], parts[2]
            try:
                link_pid = int(person_id_str)
            except ValueError:
                link_pid = None
            if link_pid:
                gid = db.join_group_with_person(invite_code, chat_id, link_pid)
                if gid:
                    p = db.get_person(link_pid)
                    g = db.get_group(gid)
                    pname = p["name"] if p else "?"
                    gname = g["name"] if g else "?"
                    # Auto-approve access if not already allowed
                    if not _auth(update):
                        db.add_allowed_chat(chat_id, pname, 0)
                    await _reply(update, ctx,
                        f"✅ Du bist der Gruppe *{gname}* als *{pname}* beigetreten!\n\n"
                        "Nutze die Buttons unten ⬇️",
                        set_active=False)
                    return
                else:
                    await _reply(update, ctx, "❌ Ungültiger Einladungslink.", set_active=False)
                    return

    if not _auth(update):
        await _request_access(update, ctx)
        return

    gid = _active_group(chat_id)
    if gid is None:
        # Auto-migration
        migrated = db.ensure_user_has_group(chat_id, default_name=(update.effective_user.first_name if update and update.effective_user else None))
        if migrated:
            gid = migrated
        else:
            # First-use flow: no groups at all
            await _reply(update, ctx,
                "👋 *Quittungs-Bot*\n\n"
                "Willkommen! Um loszulegen, erstelle eine Gruppe oder tritt einer bei:",
                inline=InlineKeyboardMarkup([
                    [InlineKeyboardButton("➕ Neue Gruppe", callback_data="group_create")],
                    [InlineKeyboardButton("🔗 Gruppe beitreten", callback_data="group_join")],
                ]),
                set_active=False)
            return

    gname = _group_name(gid)
    await _reply(update, ctx,
        f"👋 *Quittungs-Bot*\n\n"
        f"📂 Aktive Gruppe: *{gname}*\n\n"
        "📸 Foto oder PDF einer Quittung senden\n"
        "💸 Zahlung buchen\n"
        "💰 Saldo & Kontoauszuege\n"
        "👥 Personen verwalten\n\n"
        "Nutze die Buttons unten ⬇️",
        set_active=False,
    )


@_log_action("cmd_gruppe")
async def cmd_gruppe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    chat_id = update.effective_chat.id
    gid = _active_group(chat_id)
    groups = db.get_groups_for_chat(chat_id)

    if not groups:
        await _reply(update, ctx,
            "📂 *Keine Gruppen*\n\nErstelle eine Gruppe oder tritt einer bei:",
            inline=InlineKeyboardMarkup([
                [InlineKeyboardButton("➕ Neue Gruppe", callback_data="group_create")],
                [InlineKeyboardButton("🔗 Gruppe beitreten", callback_data="group_join")],
            ]),
            set_active=False)
        return

    lines = ["📂 *Gruppen:*\n"]
    rows = []
    for g in groups:
        active = " ✅" if g["id"] == gid else ""
        role = " (Admin)" if g["role"] == "admin" else ""
        lines.append(f"• *{g['name']}*{role}{active}")
        if g["id"] != gid:
            rows.append([InlineKeyboardButton(
                f"🔄 Wechseln: {g['name']}", callback_data=f"group_switch:{g['id']}")])

    rows.append([InlineKeyboardButton("➕ Neue Gruppe", callback_data="group_create")])
    rows.append([InlineKeyboardButton("🔗 Gruppe beitreten", callback_data="group_join")])
    if gid:
        rows.append([InlineKeyboardButton("📋 Einladungscode", callback_data="group_invite")])
        rows.append([InlineKeyboardButton("🚪 Gruppe verlassen", callback_data="group_leave")])

    await _reply(update, ctx, "\n".join(lines),
                 inline=InlineKeyboardMarkup(rows), set_active=False)


@_log_action("cmd_personen")
async def cmd_personen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    gid = await _ensure_group(update, ctx)
    if gid is None: return
    persons = db.get_persons(gid)
    gname = _group_name(gid)
    if not persons:
        sessions[update.effective_chat.id] = {"stage": "person_add_name"}
        await _reply(update, ctx,
            f"📂 {gname}\n\n"
            "Noch keine Personen.\n\n➕ *Name eingeben* um erste Person anzulegen:",
            set_active=True)
        return
    balances = {b["id"]: b for b in db.get_balances(_my_person_id(update.effective_chat.id, gid), gid)}
    lines = [f"📂 {gname}\n👥 *Personen & Salden:*\n"]
    for p in persons:
        b = balances.get(p["id"])
        lines.append(_fmt_balance(b) if b else f"• *{p['name']}*")
    rows = []
    my_chat = update.effective_chat.id
    for p in persons:
        row = [InlineKeyboardButton(f"📋 {p['name']}", callback_data=f"detail:{p['id']}")]
        # Status der Verknüpfung
        if p["chat_id"] == my_chat:
            row.append(InlineKeyboardButton("✓ Du", callback_data="noop"))
        elif p["chat_id"] is None:
            row.append(InlineKeyboardButton("👤 Das bin ich", callback_data=f"claim_person:{p['id']}"))
        # Einladung (für andere) + Löschen
        row.append(InlineKeyboardButton("🔗", callback_data=f"person_link:{p['id']}"))
        row.append(InlineKeyboardButton("🗑️", callback_data=f"person_del:{p['id']}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("➕ Person hinzufügen", callback_data="person_add_start")])
    await _reply(update, ctx, "\n".join(lines),
                 inline=InlineKeyboardMarkup(rows), set_active=False)


@_log_action("cmd_person_add")
async def cmd_person_add(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    gid = await _ensure_group(update, ctx)
    if gid is None: return
    if not ctx.args:
        sessions[update.effective_chat.id] = {"stage": "person_add_name"}
        await _reply(update, ctx, "➕ *Name der neuen Person eingeben:*", set_active=True)
        return
    name = " ".join(ctx.args).strip()
    db.add_person(name, gid)
    await _reply(update, ctx, f"✅ *{name}* hinzugefuegt.", set_active=False)


@_log_action("cmd_person_del")
async def cmd_person_del(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    gid = await _ensure_group(update, ctx)
    if gid is None: return
    if not ctx.args:
        persons = db.get_persons(gid)
        if not persons:
            await _reply(update, ctx, "Keine Personen vorhanden.", set_active=False)
            return
        rows = [[InlineKeyboardButton(f"🗑️ {p['name']}", callback_data=f"person_del:{p['id']}")]
                for p in persons]
        rows.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
        await _reply(update, ctx, "🗑️ *Welche Person loeschen?*",
                     inline=InlineKeyboardMarkup(rows), set_active=True)
        return
    name = " ".join(ctx.args).strip()
    match = next((p for p in db.get_persons(gid) if p["name"].lower() == name.lower()), None)
    if not match:
        await _reply(update, ctx, f"❌ '{name}' nicht gefunden.")
        return
    db.delete_person(match["id"])
    await _reply(update, ctx, f"🗑️ *{match['name']}* entfernt.", set_active=False)


@_log_action("cmd_saldo")
async def cmd_saldo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    gid = await _ensure_group(update, ctx)
    if gid is None: return
    balances = db.get_balances(_my_person_id(update.effective_chat.id, gid), gid)
    gname = _group_name(gid)
    if not balances:
        await _reply(update, ctx,
            f"📂 {gname}\n\nKeine Daten.\n/person_add [Name]", set_active=False)
        return
    lines = [f"📂 {gname}\n💰 *Aktuelle Salden:*\n"]
    for b in balances:
        lines.append(_fmt_balance(b))
        details = []
        if b["owed_me"] > 0:  details.append(f"Quitt. von dir: +{CURRENCY} {b['owed_me']:.2f}")
        if b["i_owe"] > 0:    details.append(f"Quitt. von ihnen: -{CURRENCY} {b['i_owe']:.2f}")
        if b["received"] > 0: details.append(f"Erhalten: -{CURRENCY} {b['received']:.2f}")
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


@_log_action("cmd_detail")
async def cmd_detail(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    gid = await _ensure_group(update, ctx)
    if gid is None: return
    if ctx.args:
        name = " ".join(ctx.args).strip()
        p = next((x for x in db.get_persons(gid) if x["name"].lower() == name.lower()), None)
        if not p:
            await _reply(update, ctx, f"❌ '{name}' nicht gefunden.")
            return
        await _send_detail(ctx, update.effective_chat.id, p["id"])
    else:
        sessions[update.effective_chat.id] = {"stage": "detail_select"}
        await _reply(update, ctx, "Fuer wen?",
                     inline=_simple_persons_kbd("detail", group_id=gid),
                     set_active=True)


async def _send_detail(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, person_id: int):
    gid = _active_group(chat_id)
    hist     = db.get_person_history(person_id)
    balances = {b["id"]: b for b in db.get_balances(_my_person_id(chat_id, gid), gid)}
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
        lines.append("_Noch keine Eintraege._")

    await _send(ctx, chat_id, "\n".join(lines), set_active=False)


@_log_action("cmd_verlauf")
async def cmd_verlauf(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    gid = await _ensure_group(update, ctx)
    if gid is None: return
    transfers = db.get_cash_transfers(limit=20, group_id=gid)
    gname = _group_name(gid)
    if not transfers:
        await _reply(update, ctx, f"📂 {gname}\n\nNoch keine Zahlungen.", set_active=False)
        return
    lines = [f"📂 {gname}\n💸 *Letzte Zahlungen:*\n"]
    for t in transfers:
        arrow = "📥 von" if t["direction"] == "received" else "📤 an"
        note  = f" · _{t['note']}_" if t.get("note") else ""
        date  = str(t.get("created_at", ""))[:10]
        lines.append(f"[{t['id']}] {arrow} *{t['name']}*: {CURRENCY} {t['amount']:.2f}{note} [{date}]")
    rows = [[InlineKeyboardButton("🗑️ Letzte Zahlung loeschen", callback_data="del:last_transfer")]]
    await _reply(update, ctx, "\n".join(lines),
                 inline=InlineKeyboardMarkup(rows), set_active=False)


@_log_action("cmd_quittungen")
async def cmd_quittungen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    gid = await _ensure_group(update, ctx)
    if gid is None: return
    receipts = db.get_receipts(10, gid)
    gname = _group_name(gid)
    if not receipts:
        await _reply(update, ctx, f"📂 {gname}\n\nNoch keine Quittungen.", set_active=False)
        return
    lines = [f"📂 {gname}\n🧾 *Letzte Quittungen:*\n"]
    for r in receipts:
        date  = (r["date"] or str(r["uploaded_at"])[:10])
        store = r["store"] or "Unbekannt"
        _cur = r['currency'] or CURRENCY
        total = f"{_cur} {r['total']:.2f}" if r["total"] else "?"
        payer = f"gezahlt von *{r['payer_name']}*" if r["payer_name"] else "von *mir* gezahlt"
        lines.append(f"• *{store}* – {date} – {total} · {payer}")
    rows = [[InlineKeyboardButton(
        f"📋 {(r['store'] or 'Quittung')[:20]} – {(r['date'] or '')[:10]}",
        callback_data=f"receipt_detail:{r['id']}"
    )] for r in receipts]
    rows.append([InlineKeyboardButton("🗑️ Letzte Quittung loeschen", callback_data="del:last_receipt")])
    await _reply(update, ctx, "\n".join(lines),
                 inline=InlineKeyboardMarkup(rows), set_active=False)


@_log_action("cmd_geld")
async def cmd_geld(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    gid = await _ensure_group(update, ctx)
    if gid is None: return
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


@_log_action("cmd_loeschen")
async def cmd_loeschen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    await _reply(update, ctx, "Was loeschen?",
        inline=InlineKeyboardMarkup([
            [InlineKeyboardButton("💸 Letzte Zahlung",  callback_data="del:last_transfer")],
            [InlineKeyboardButton("🧾 Letzte Quittung", callback_data="del:last_receipt")],
            [InlineKeyboardButton("❌ Abbrechen",       callback_data="cancel")],
        ]),
        set_active=True,
    )


@_log_action("cmd_posten")
async def cmd_posten(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    gid = await _ensure_group(update, ctx)
    if gid is None: return
    sessions[update.effective_chat.id] = {"stage": "posten_desc"}
    await _reply(update, ctx,
        "➕ *Manueller Posten*\n\n📝 Beschreibung eingeben (z.B. _Pizza bestellt_):",
        set_active=True)


@_log_action("cmd_abbrechen")
async def cmd_abbrechen(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update): return
    sessions.pop(update.effective_chat.id, None)
    await _reply(update, ctx, "❌ Abgebrochen.", set_active=False)


# ── Datei-Handler ─────────────────────────────────────────────────────────────

@_log_action("handle_file")
async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        await _request_access(update, ctx)
        return
    chat_id = update.effective_chat.id
    gid = _active_group(chat_id)
    if gid is None:
        migrated = db.ensure_user_has_group(chat_id, default_name=(update.effective_user.first_name if update and update.effective_user else None))
        if migrated:
            gid = migrated
        else:
            await _reply(update, ctx,
                "📂 *Keine Gruppe aktiv*\n\nNutze /gruppe um eine Gruppe zu erstellen.",
                set_active=False)
            return

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
        await msg.delete()
        await _send(ctx, chat_id, "❌ Nicht unterstuetzter Dateityp.", set_active=False)
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
        await msg.delete()
        await _send(ctx, chat_id, f"❌ KI-Fehler: {e}", set_active=False)
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
        group_id = gid,
    )

    raw_items = data.get("items", [])
    if not raw_items:
        await msg.delete()
        await _send(ctx, chat_id,
            f"⚠️ Keine Positionen erkannt.\n"
            f"#{receipt_id} – {data.get('store','?')} – {data.get('currency',CURRENCY)} {data.get('total',0):.2f}",
            set_active=False)
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
        qty = f"x{int(it['quantity'])} " if it["quantity"] != 1 else ""
        lines.append(f"  {i}. {it['description']} {qty}-> {cur} {it['amount']:.2f}")

    text = (
        f"🧾 *{data.get('store','?')}*"
        + (f" – {data['date']}" if data.get("date") else "")
        + f"\n\n*Positionen:*\n" + "\n".join(lines)
        + f"\n\n💰 *Total: {cur} {total:.2f}*"
        + "\n\n❓ *Wer hat gezahlt?*"
    )
    persons = db.get_persons(gid)
    kbd = [[InlineKeyboardButton("👤 Ich habe gezahlt", callback_data="payer:me")]]
    for p in persons:
        kbd.append([InlineKeyboardButton(f"💳 {p['name']} hat gezahlt",
                                         callback_data=f"payer:{p['id']}")])
    kbd.append([InlineKeyboardButton("➕ Person hinzufügen", callback_data="inline_add_person")])
    kbd.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
    await msg.delete()
    await ctx.bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(kbd), parse_mode="Markdown")


# ── Zuweisung Keyboards ───────────────────────────────────────────────────────

def _assign_start_kbd(payer_is_me: bool, group_id: int | None = None,
                       chat_id: int | None = None) -> InlineKeyboardMarkup:
    persons = db.get_persons(group_id)
    my_pid = _my_person_id(chat_id, group_id) if chat_id else None
    rows = []
    # Self-assign zuerst — prominent
    if my_pid is not None:
        rows.append([InlineKeyboardButton(
            "👤 Alles -> ICH (komplett selbst)" if payer_is_me else "👤 Alles konsumiert: ICH",
            callback_data=f"assign_one:{my_pid}"
        )])
    rows.append([InlineKeyboardButton(
        "👥 Personen waehlen (gleich aufteilen)", callback_data="assign_pick_all"
    )])
    for p in persons:
        if p["id"] == my_pid:
            continue  # bereits oben als "ICH"
        label = f"Alles -> {p['name']}" if payer_is_me else f"Alles konsumiert: {p['name']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"assign_one:{p['id']}")])
    rows.append([InlineKeyboardButton("➕ Person hinzufügen", callback_data="inline_add_person")])
    # Projekte (global, optional) — alles dem Projekt zuweisen
    for pj in db.get_projects(include_archived=False):
        label = f"📁 Alles -> Projekt: {pj['name']}"
        rows.append([InlineKeyboardButton(label, callback_data=f"assign_project:{pj['id']}")])
    rows.append([InlineKeyboardButton("🔀 Manuell (Position fuer Position)",
                                      callback_data="assign_manual")])
    rows.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
    return InlineKeyboardMarkup(rows)


# ── Callback-Handler ──────────────────────────────────────────────────────────

@_log_action("handle_callback")
async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query   = update.callback_query
    await query.answer()
    chat_id = update.effective_chat.id
    data    = query.data
    session = sessions.get(chat_id, {})
    gid     = _active_group(chat_id)

    if data == "noop":
        return

    # ── Projekt: Liste / Detail / Add / Archive / Delete
    if data == "project_list":
        all_projects = db.get_projects(include_archived=True)
        active = [p for p in all_projects if not p["archived"]]
        archived = [p for p in all_projects if p["archived"]]
        lines = ["📁 *Projekte*\n", f"*Aktiv* ({len(active)})"]
        if not active:
            lines.append("_keine_")
        else:
            for pj in active:
                total = db.get_project_total(pj["id"])
                lines.append(f"• *{pj['name']}* — {total:.2f}")
        lines.append("")
        if archived:
            lines.append(f"*Archiviert* ({len(archived)})")
            for pj in archived:
                lines.append(f"• {pj['name']}")
        rows = []
        for pj in active[:8]:
            rows.append([
                InlineKeyboardButton(f"📋 {pj['name']}", callback_data=f"project_detail:{pj['id']}"),
                InlineKeyboardButton("📦", callback_data=f"project_archive:{pj['id']}"),
                InlineKeyboardButton("🗑", callback_data=f"project_delete:{pj['id']}"),
            ])
        rows.append([InlineKeyboardButton("➕ Neues Projekt", callback_data="project_add")])
        rows.append([InlineKeyboardButton("🔄 Aktualisieren", callback_data="project_list")])
        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    if data == "project_add":
        session["stage"] = "project_add_name"
        sessions[chat_id] = session
        await query.edit_message_text(
            "📁 Wie soll das neue Projekt heissen?",
            parse_mode="Markdown")
        return

    if data.startswith("project_archive:"):
        pj_id = int(data.split(":")[1])
        db.archive_project(pj_id)
        await query.answer("Archiviert")
        # Re-render
        query.data = "project_list"
        await handle_callback(update, ctx)
        return

    if data.startswith("project_delete:"):
        pj_id = int(data.split(":")[1])
        db.delete_project(pj_id)
        await query.answer("Gelöscht")
        query.data = "project_list"
        await handle_callback(update, ctx)
        return

    if data.startswith("project_detail:"):
        pj_id = int(data.split(":")[1])
        pj = db.get_project(pj_id)
        items = db.get_project_items(pj_id)
        total = db.get_project_total(pj_id)
        lines = [f"📁 *Projekt: {pj['name']}*", f"Total: *{total:.2f}*\n"]
        if not items:
            lines.append("_Keine Posten zugewiesen._")
        else:
            for it in items[:20]:
                lines.append(
                    f"• *{it['description']}* — {it['share_amount']:.2f} "
                    f"_({it['store'] or '?'}, {it['date'] or '?'})_"
                )
            if len(items) > 20:
                lines.append(f"\n_… und {len(items)-20} weitere_")
        rows = [
            [InlineKeyboardButton("📦 Archivieren", callback_data=f"project_archive:{pj_id}"),
             InlineKeyboardButton("🗑 Löschen", callback_data=f"project_delete:{pj_id}")],
            [InlineKeyboardButton("« Zurück", callback_data="project_list")],
        ]
        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows))
        return

    # ── Admin: User per Inline-Button sperren
    if data.startswith("user_revoke:"):
        if not _is_admin(chat_id):
            await query.answer("Nur Admins.", show_alert=True)
            return
        target = int(data.split(":")[1])
        if target in ALLOWED_CHAT_IDS:
            await query.answer("Admin kann nicht via Bot gesperrt werden.", show_alert=True)
            return
        db.remove_allowed_chat(target)
        _pending_requests.discard(target)
        await query.edit_message_text(
            f"🚫 User `{target}` gesperrt.", parse_mode="Markdown"
        )
        try:
            await ctx.bot.send_message(target, "🚫 Dein Zugriff wurde widerrufen.")
        except Exception:
            pass
        return

    # ── Admin: Refresh /users View
    if data == "show_users":
        if not _is_admin(chat_id):
            await query.answer("Nur Admins.", show_alert=True)
            return
        admins = list(ALLOWED_CHAT_IDS)
        approved = db.get_allowed_chats_full()
        pending = sorted(_pending_requests)
        lines = ["👥 *User-Verwaltung*\n"]
        lines.append(f"*👑 Admins* ({len(admins)})")
        for aid in admins:
            lines.append(f"• `{aid}`")
        lines.append("")
        lines.append(f"*✅ Approved* ({len(approved)})")
        if approved:
            for u in approved:
                lines.append(f"• *{u.get('name') or '?'}* — `{u['chat_id']}`")
        else:
            lines.append("_keine_")
        lines.append("")
        lines.append(f"*⏳ Pending* ({len(pending)})")
        if pending:
            for pid in pending:
                lines.append(f"• `{pid}`")
        else:
            lines.append("_keine_")
        rows = []
        for pid in pending[:5]:
            rows.append([
                InlineKeyboardButton(f"✅ Erlauben {pid}", callback_data=f"access_grant:{pid}:#{pid}"),
                InlineKeyboardButton(f"❌ Ablehnen {pid}", callback_data=f"access_deny:{pid}"),
            ])
        for u in approved[:8]:
            name = (u.get("name") or str(u["chat_id"]))[:18]
            rows.append([InlineKeyboardButton(
                f"🚫 Sperren: {name}", callback_data=f"user_revoke:{u['chat_id']}"
            )])
        rows.append([InlineKeyboardButton("🔄 Aktualisieren", callback_data="show_users")])
        await query.edit_message_text(
            "\n".join(lines), parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(rows) if rows else None
        )
        return

    # ── Zugriffsanfrage: Erlauben
    if data.startswith("access_grant:"):
        parts = data.split(":", 2)
        req_chat_id = int(parts[1])
        req_name = parts[2] if len(parts) > 2 else str(req_chat_id)
        if not _is_admin(chat_id):
            await query.answer("Nur Admins koennen Zugriff erteilen.", show_alert=True)
            return
        db.add_allowed_chat(req_chat_id, req_name, chat_id)
        _pending_requests.discard(req_chat_id)
        await query.edit_message_text(
            f"✅ *{req_name}* (Chat-ID: `{req_chat_id}`) wurde freigeschaltet.",
            parse_mode="Markdown"
        )
        try:
            await ctx.bot.send_message(req_chat_id,
                "✅ Dein Zugriff wurde freigeschaltet! Schick /start um loszulegen.")
        except Exception:
            log.warning("Could not notify user %s", req_chat_id)
        return

    # ── Zugriffsanfrage: Ablehnen
    if data.startswith("access_deny:"):
        req_chat_id = int(data.split(":")[1])
        if not _is_admin(chat_id):
            await query.answer("Nur Admins koennen Zugriff verweigern.", show_alert=True)
            return
        _pending_requests.discard(req_chat_id)
        await query.edit_message_text("❌ Anfrage abgelehnt.")
        try:
            await ctx.bot.send_message(req_chat_id,
                "❌ Dein Zugriff wurde abgelehnt.")
        except Exception:
            log.warning("Could not notify user %s", req_chat_id)
        return

    # ── Group callbacks ──────────────────────────────────────────────────────

    if data == "group_create":
        session["stage"] = "group_create_name"
        sessions[chat_id] = session
        await query.edit_message_text(
            "➕ *Neue Gruppe erstellen*\n\nName der Gruppe eingeben:",
            parse_mode="Markdown")
        return

    if data == "group_join":
        session["stage"] = "group_join_code"
        sessions[chat_id] = session
        await query.edit_message_text(
            "🔗 *Gruppe beitreten*\n\nEinladungscode eingeben:",
            parse_mode="Markdown")
        return

    if data.startswith("group_switch:"):
        new_gid = int(data.split(":")[1])
        db.set_active_group(chat_id, new_gid)
        g = db.get_group(new_gid)
        gname = g["name"] if g else "?"
        sessions.pop(chat_id, None)
        await query.edit_message_text(
            f"✅ Aktive Gruppe: *{gname}*", parse_mode="Markdown")
        await _send(ctx, chat_id, f"📂 Gruppe gewechselt zu *{gname}*.", set_active=False)
        return

    if data == "group_invite":
        if gid is None:
            await query.edit_message_text("❌ Keine aktive Gruppe.")
            return
        g = db.get_group(gid)
        if g:
            await query.edit_message_text(
                f"📋 *Einladungscode fuer {g['name']}:*\n\n"
                f"`{g['invite_code']}`\n\n"
                f"Teile diesen Code, damit andere beitreten koennen.",
                parse_mode="Markdown")
        return

    if data == "group_leave":
        if gid is None:
            await query.edit_message_text("❌ Keine aktive Gruppe.")
            return
        g = db.get_group(gid)
        gname = g["name"] if g else "?"
        db.leave_group(chat_id, gid)
        sessions.pop(chat_id, None)
        new_gid = _active_group(chat_id)
        if new_gid:
            new_g = db.get_group(new_gid)
            new_name = new_g["name"] if new_g else "?"
            await query.edit_message_text(
                f"🚪 Du hast *{gname}* verlassen.\n"
                f"Aktive Gruppe: *{new_name}*",
                parse_mode="Markdown")
        else:
            await query.edit_message_text(
                f"🚪 Du hast *{gname}* verlassen.\n\n"
                f"Du bist in keiner Gruppe mehr. Nutze /gruppe um eine neue zu erstellen.",
                parse_mode="Markdown")
        await _send(ctx, chat_id, "Bereit.", set_active=False)
        return

    # ── Inline Person hinzufuegen
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

    # ── Person hinzufuegen (aus Personen-Menue)
    if data == "person_add_start":
        session["stage"] = "person_add_name"
        sessions[chat_id] = session
        await query.edit_message_text("➕ *Name der neuen Person eingeben:*",
                                      parse_mode="Markdown")
        return

    # ── Person loeschen
    # ── Person verknüpfen: Einladungslink generieren
    # ── Self-Link: "Das bin ich"
    if data.startswith("claim_person:"):
        pid = int(data.split(":")[1])
        p = db.get_person(pid)
        if not p:
            await query.edit_message_text("❌ Person nicht gefunden.")
            return
        if p["chat_id"] is not None and p["chat_id"] != chat_id:
            await query.answer("Person ist bereits jemandem anderem zugeordnet.", show_alert=True)
            return
        # Link
        conn = db.get_conn()
        conn.execute("UPDATE persons SET chat_id=? WHERE id=?", (chat_id, pid))
        conn.execute(
            "UPDATE group_members SET person_id=? WHERE chat_id=? AND group_id=?",
            (pid, chat_id, p["group_id"])
        )
        conn.commit()
        conn.close()
        await query.edit_message_text(
            f"✅ Du bist jetzt verlinkt mit *{p['name']}* in dieser Gruppe.\n"
            f"Tap /personen um es zu sehen.",
            parse_mode="Markdown"
        )
        return

    if data.startswith("person_link:"):
        pid = int(data.split(":")[1])
        p = db.get_person(pid)
        if not p:
            await query.edit_message_text("❌ Person nicht gefunden.")
            return
        gid = _active_group(chat_id)
        g = db.get_group(gid) if gid else None
        if not g:
            await query.edit_message_text("❌ Keine aktive Gruppe.")
            return
        invite_code = g["invite_code"]
        bot_info = await ctx.bot.get_me()
        link = f"https://t.me/{bot_info.username}?start=join_{invite_code}_{pid}"
        await query.edit_message_text(
            f"🔗 *Einladungslink für {p['name']}:*\n\n"
            f"`{link}`\n\n"
            f"Schicke diesen Link an *{p['name']}*.\n"
            f"Wenn sie/er draufklickt, wird automatisch die Person verknüpft.",
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

    # ── Quittung Detail
    if data.startswith("receipt_detail:"):
        rid = int(data.split(":")[1])
        receipts = db.get_receipts(50, gid)
        r = next((x for x in receipts if x["id"] == rid), None)
        if not r:
            await query.edit_message_text("❌ Quittung nicht gefunden.")
            return
        items = db.get_items_for_receipt(rid)
        date = (r["date"] or str(r["uploaded_at"])[:10])
        store = r["store"] or "Unbekannt"
        payer = f"*{r['payer_name']}*" if r["payer_name"] else "*mir*"
        lines = [
            f"🧾 *{store}* – {date}",
            f"💰 Total: {r['currency'] or CURRENCY} {r['total']:.2f}",
            f"💳 Gezahlt von: {payer}\n",
            "*Positionen:*",
        ]
        for it in items:
            assigned = it["person_names"] or "—"
            qty = f"x{int(it['quantity'])} " if it["quantity"] and it["quantity"] != 1 else ""
            lines.append(f"  • {it['description']} {qty}-> {r['currency'] or CURRENCY} {it['amount']:.2f}")
            lines.append(f"    ↳ {assigned}")
        kbd = [
            [InlineKeyboardButton("💳 Zahler aendern", callback_data=f"edit_payer:{rid}")],
            [InlineKeyboardButton("🔀 Zuweisungen aendern", callback_data=f"edit_assign:{rid}")],
            [InlineKeyboardButton("🗑️ Quittung loeschen", callback_data=f"del:receipt:{rid}")],
        ]
        await query.edit_message_text("\n".join(lines),
                                      reply_markup=InlineKeyboardMarkup(kbd),
                                      parse_mode="Markdown")
        return

    # ── Quittung: Zahler aendern
    if data.startswith("edit_payer:"):
        rid = int(data.split(":")[1])
        session.update({"stage": "edit_payer_select", "edit_receipt_id": rid})
        sessions[chat_id] = session
        persons = db.get_persons(gid)
        kbd = [[InlineKeyboardButton("👤 Ich habe gezahlt", callback_data=f"set_payer:{rid}:me")]]
        for p in persons:
            kbd.append([InlineKeyboardButton(
                f"💳 {p['name']}", callback_data=f"set_payer:{rid}:{p['id']}"
            )])
        kbd.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
        await query.edit_message_text("💳 *Wer hat gezahlt?*",
                                      reply_markup=InlineKeyboardMarkup(kbd),
                                      parse_mode="Markdown")
        return

    if data.startswith("set_payer:"):
        parts = data.split(":")
        rid = int(parts[1])
        who = parts[2]
        if who == "me":
            db.update_receipt_payer(rid, None, 0.0)
            await query.edit_message_text("✅ Zahler auf *dich* geaendert.", parse_mode="Markdown")
        else:
            pid = int(who)
            p = db.get_person(pid)
            pname = p["name"] if p else "?"
            # Recalculate my_share
            total_r = db.get_receipts(50, gid)
            r = next((x for x in total_r if x["id"] == rid), None)
            total = r["total"] if r else 0
            assigned = db.get_total_assigned(rid)
            my_share = round(max(total - assigned, 0), 2)
            db.update_receipt_payer(rid, pid, my_share)
            await query.edit_message_text(
                f"✅ Zahler auf *{pname}* geaendert.", parse_mode="Markdown")
        sessions.pop(chat_id, None)
        await _send(ctx, chat_id, "Erledigt.", set_active=False)
        return

    # ── Quittung: Zuweisungen aendern
    if data.startswith("edit_assign:"):
        rid = int(data.split(":")[1])
        items_raw = db.get_items_for_receipt(rid)
        receipts = db.get_receipts(50, gid)
        r = next((x for x in receipts if x["id"] == rid), None)
        items_s = [
            {"id": it["id"], "description": it["description"],
             "amount": it["amount"], "quantity": it["quantity"] or 1}
            for it in items_raw
        ]
        session.update({
            "stage": "manual",
            "receipt_id": rid,
            "items": items_s,
            "data": {"currency": r["currency"] if r else CURRENCY,
                     "total": r["total"] if r else 0},
            "payer_id": r["payer_id"] if r else None,
            "payer_name": r["payer_name"] if r and r["payer_name"] else "ich",
            "manual_idx": 0,
            "selected_item": [],
        })
        sessions[chat_id] = session
        await _show_manual_item(query, session, gid)
        return

    # ── Quittung: einzelne loeschen
    if data.startswith("del:receipt:"):
        rid = int(data.split(":")[2])
        receipts = db.get_receipts(50, gid)
        r = next((x for x in receipts if x["id"] == rid), None)
        if r:
            db.delete_receipt(rid)
            await query.edit_message_text(
                f"🗑️ Quittung geloescht: *{r['store'] or 'Quittung'}* {CURRENCY} {r['total']:.2f}",
                parse_mode="Markdown")
        else:
            await query.edit_message_text("❌ Quittung nicht gefunden.")
        sessions.pop(chat_id, None)
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
        p_map = {p["id"]: p["name"] for p in db.get_persons(gid)}

        if who == "me":
            db.update_receipt_payer(session["receipt_id"], None, 0.0)
            session.update({"payer_id": None, "payer_name": "ich", "stage": "assign_method"})
            sessions[chat_id] = session
            await query.edit_message_text(
                f"✅ Du hast gezahlt ({cur} {total:.2f}).\n\n*Wem werden die Kosten zugewiesen?*",
                reply_markup=_assign_start_kbd(payer_is_me=True, group_id=gid, chat_id=chat_id),
                parse_mode="Markdown"
            )
        else:
            pid = int(who)
            pname = p_map.get(pid, "?")
            session.update({"payer_id": pid, "payer_name": pname, "stage": "assign_method"})
            sessions[chat_id] = session
            db.update_receipt_payer(session["receipt_id"], pid, 0.0)
            await query.edit_message_text(
                f"💳 *{pname}* hat {cur} {total:.2f} gezahlt.\n\n"
                f"*Wem werden die Kosten zugewiesen?*\n"
                f"_(Was nicht zugewiesen wird = dein Anteil)_",
                reply_markup=_assign_start_kbd(payer_is_me=False, group_id=gid, chat_id=chat_id),
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

    # ── ZUWEISUNG: ein Projekt (global, alle Items dorthin)
    if data.startswith("assign_project:"):
        pj_id = int(data.split(":")[1])
        items = session.get("items", [])
        for item in items:
            db.assign_item_to_project(item["id"], pj_id, item["amount"])
        # plus dem Bezahler (falls "me") als virtuelle Person aufgeführt? Optional.
        # Hier: nur Projekt-Zuweisung, kein Person-Saldo dadurch.
        pj = db.get_project(pj_id)
        await query.edit_message_text(
            f"✅ Alle Posten zugewiesen an Projekt: *{pj['name']}*",
            parse_mode="Markdown"
        )
        sessions.pop(chat_id, None)
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
            f"_(Mehrere waehlbar)_",
            reply_markup=_persons_toggle_kbd(
                [], "pick_all_toggle",
                confirm_label="✅ Aufteilen",
                confirm_cb="pick_all_confirm",
                group_id=gid
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
        share_info = f"\n-> {cur} {each:.2f} je Person" if n else ""
        await query.edit_message_reply_markup(
            reply_markup=_persons_toggle_kbd(
                selected, "pick_all_toggle",
                confirm_label=f"✅ Aufteilen{share_info}",
                confirm_cb="pick_all_confirm",
                group_id=gid
            )
        )
        return

    if data == "pick_all_confirm":
        selected = session.get("selected_all", [])
        if not selected:
            await query.answer("Mindestens eine Person auswaehlen!", show_alert=True)
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
        await _show_manual_item(query, session, gid)
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
        share_info = f"\n-> {cur} {each:.2f} je" if n else ""
        await query.edit_message_reply_markup(
            reply_markup=_persons_toggle_kbd(
                selected, "itm_toggle",
                confirm_label=f"✅ Zuweisen{share_info}",
                confirm_cb="itm_confirm",
                extra=[[InlineKeyboardButton("⏭ Ueberspringen", callback_data="skip_item")]],
                group_id=gid
            )
        )
        return

    if data == "itm_confirm":
        selected = session.get("selected_item", [])
        if not selected:
            await query.answer("Mindestens eine Person auswaehlen!", show_alert=True)
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
            await _show_manual_item(query, session, gid)
        return

    if data == "skip_item":
        session["manual_idx"]   = session.get("manual_idx", 0) + 1
        session["selected_item"] = []
        sessions[chat_id] = session
        items = session.get("items", [])
        if session["manual_idx"] >= len(items):
            await _finish_assignment(query, ctx, chat_id, session, items)
        else:
            await _show_manual_item(query, session, gid)
        return

    # ── GELD: Richtung
    if data.startswith("geld_dir:"):
        direction = data.split(":")[1]
        session.update({"direction": direction, "stage": "geld_person"})
        sessions[chat_id] = session
        label = "gibt dir Geld" if direction == "received" else "bekommt Geld"
        await query.edit_message_text(
            f"💸 Wer {label}?",
            reply_markup=_simple_persons_kbd("geld_person", group_id=gid)
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

    # ── LOESCHEN
    if data == "del:last_transfer":
        transfers = db.get_cash_transfers(limit=1, group_id=gid)
        sessions.pop(chat_id, None)
        if transfers:
            t = transfers[0]
            db.delete_cash_transfer(t["id"])
            await query.edit_message_text(
                f"🗑️ Zahlung geloescht: *{t['name']}* {CURRENCY} {t['amount']:.2f}",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Keine Zahlungen vorhanden.")
        await _send(ctx, chat_id, "Erledigt.", set_active=False)
        return

    if data == "del:last_receipt":
        receipts = db.get_receipts(1, gid)
        sessions.pop(chat_id, None)
        if receipts:
            r = receipts[0]
            db.delete_receipt(r["id"])
            await query.edit_message_text(
                f"🗑️ Quittung geloescht: *{r['store'] or 'Quittung'}* {CURRENCY} {r['total']:.2f}",
                parse_mode="Markdown"
            )
        else:
            await query.edit_message_text("Keine Quittungen vorhanden.")
        await _send(ctx, chat_id, "Erledigt.", set_active=False)
        return

    # ── Notiz ueberspringen
    if data == "note_skip":
        stage = session.get("stage")
        if stage == "geld_note":
            await _save_geld(ctx, chat_id, session, note="")
        return


# ── Hilfsfunktionen ───────────────────────────────────────────────────────────

async def _rebuild_person_keyboard(ctx: ContextTypes.DEFAULT_TYPE,
                                   chat_id: int, session: dict, stage: str):
    """Nach Inline-Person-Erstellung das passende Keyboard neu senden."""
    gid = _active_group(chat_id)
    items = session.get("items", [])
    cur = session.get("data", {}).get("currency", CURRENCY) if session.get("data") else CURRENCY

    if stage == "payer_select":
        total = session.get("data", {}).get("total") or sum(i["amount"] for i in items)
        persons = db.get_persons(gid)
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
                    inline=_assign_start_kbd(payer_is_me=payer_is_me, group_id=gid, chat_id=chat_id))

    elif stage == "pick_all":
        selected = session.get("selected_all", [])
        total = sum(i["amount"] for i in items)
        n = len(selected)
        each = total / n if n else 0
        share_info = f"\n-> {cur} {each:.2f} je Person" if n else ""
        await _send(ctx, chat_id,
            f"👥 *Wer teilt sich die Kosten?*\nTotal: {cur} {total:.2f}\n_(Mehrere waehlbar)_",
            inline=_persons_toggle_kbd(
                selected, "pick_all_toggle",
                confirm_label=f"✅ Aufteilen{share_info}",
                confirm_cb="pick_all_confirm",
                group_id=gid
            ))

    elif stage == "manual":
        selected = session.get("selected_item", [])
        idx = session.get("manual_idx", 0)
        if idx < len(items):
            item = items[idx]
            payer_is_me = session.get("payer_id") is None
            qty_txt = f"x{int(item['quantity'])} " if item["quantity"] != 1 else ""
            hint = "Wem wird zugewiesen?" if payer_is_me else f"Wer hat's konsumiert?"
            n = len(selected)
            each = item["amount"] / n if n else 0
            share_info = f"\n-> {cur} {each:.2f} je" if n else ""
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
                            extra=[[InlineKeyboardButton("⏭ Ueberspringen", callback_data="skip_item")]],
                            group_id=gid
                        ))

    elif stage == "geld_person":
        d = session.get("direction", "received")
        label = "gibt dir Geld" if d == "received" else "bekommt Geld"
        await _send(ctx, chat_id, f"💸 Wer {label}?",
                    inline=_simple_persons_kbd("geld_person", group_id=gid))

    elif stage == "detail_select":
        await _send(ctx, chat_id, "Fuer wen?",
                    inline=_simple_persons_kbd("detail", group_id=gid))


async def _show_manual_item(query, session: dict, group_id: int | None = None):
    items    = session.get("items", [])
    idx      = session.get("manual_idx", 0)
    item     = items[idx]
    selected = session.get("selected_item", [])
    cur      = session.get("data", {}).get("currency", CURRENCY)
    payer_is_me = session.get("payer_id") is None
    qty_txt  = f"x{int(item['quantity'])} " if item["quantity"] != 1 else ""
    hint = "Wem wird zugewiesen?" if payer_is_me else f"Wer hat's konsumiert? (schuldet {session.get('payer_name','?')})"
    n    = len(selected)
    each = item["amount"] / n if n else 0
    share_info = f"\n-> {cur} {each:.2f} je" if n else ""

    text = (
        f"📦 *Position {idx+1}/{len(items)}*\n\n"
        f"*{item['description']}* {qty_txt}\n"
        f"💶 {cur} {item['amount']:.2f}\n\n"
        f"_{hint}_\n"
        f"_(Mehrere Personen waehlbar)_"
    )
    await query.edit_message_text(
        text,
        reply_markup=_persons_toggle_kbd(
            selected, "itm_toggle",
            confirm_label=f"✅ Zuweisen{share_info}",
            confirm_cb="itm_confirm",
            extra=[[InlineKeyboardButton("⏭ Ueberspringen", callback_data="skip_item")]],
            group_id=group_id
        ),
        parse_mode="Markdown"
    )


async def _finish_assignment(query, ctx: ContextTypes.DEFAULT_TYPE,
                              chat_id: int, session: dict, items: list):
    sessions.pop(chat_id, None)
    payer_name  = session.get("payer_name", "ich")
    cur         = session.get("data", {}).get("currency", CURRENCY) if session.get("data") else CURRENCY
    total       = session.get("data", {}).get("total") or sum(i["amount"] for i in items)
    payer_is_me = session.get("payer_id") is None

    if payer_is_me:
        msg = (f"✅ *{len(items)} Positionen* ({cur} {total:.2f}) zugewiesen.\n"
               f"💳 Bezahlt von: *dir*")
    else:
        # my_share = total - was anderen zugewiesen wurde
        assigned = db.get_total_assigned(session["receipt_id"])
        my_share = round(max(total - assigned, 0), 2)
        db.update_receipt_payer(session["receipt_id"], session["payer_id"], my_share)
        msg = (f"✅ *{len(items)} Positionen* zugewiesen.\n"
               f"💳 Bezahlt von: *{payer_name}*\n"
               f"📌 Dein Anteil: {cur} {my_share:.2f}")

    await query.edit_message_text(msg, parse_mode="Markdown")
    await _send(ctx, chat_id, "Gespeichert. /saldo fuer Uebersicht.", set_active=False)


async def _save_geld(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int,
                     session: dict, note: str = ""):
    gid = _active_group(chat_id)
    person_id = session["person_id"]
    amount    = session["amount"]
    direction = session["direction"]
    sessions.pop(chat_id, None)
    db.add_cash_transfer(person_id, amount, direction, note, group_id=gid)
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

async def _request_access(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Sendet Zugriffsanfrage an alle Admins."""
    chat_id = update.effective_chat.id
    if chat_id in _pending_requests:
        await update.message.reply_text("⏳ Deine Anfrage wurde bereits gesendet. Bitte warte auf Freigabe.")
        return
    _pending_requests.add(chat_id)
    user = update.effective_user
    name = user.full_name if user else str(chat_id)
    username = f" (@{user.username})" if user and user.username else ""
    await update.message.reply_text("🔒 Du hast keinen Zugriff.\n\nEine Anfrage wurde an den Admin gesendet.")
    for admin_id in ALLOWED_CHAT_IDS:
        try:
            await ctx.bot.send_message(
                admin_id,
                f"🔔 *Zugriffsanfrage*\n\n"
                f"*{name}*{username}\n"
                f"Chat-ID: `{chat_id}`",
                parse_mode="Markdown",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("✅ Erlauben", callback_data=f"access_grant:{chat_id}:{name}")],
                    [InlineKeyboardButton("❌ Ablehnen", callback_data=f"access_deny:{chat_id}")],
                ])
            )
        except Exception:
            log.warning("Could not notify admin %s", admin_id)


@_log_action("handle_text")
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _auth(update):
        await _request_access(update, ctx)
        return
    chat_id = update.effective_chat.id
    text    = update.message.text.strip()

    # Reply-Keyboard Buttons
    if text in REPLY_TRIGGERS:
        cmd = REPLY_TRIGGERS[text]
        ctx.args = []
        handlers = {
            "geld":       cmd_geld,
            "posten":     cmd_posten,
            "saldo":      cmd_saldo,
            "detail":     cmd_detail,
            "verlauf":    cmd_verlauf,
            "quittungen": cmd_quittungen,
            "personen":   cmd_personen,
            "gruppe":     cmd_gruppe,
            "projekte":   cmd_projekte,
            "loeschen":   cmd_loeschen,
            "users":      cmd_users,
            "abbrechen":  cmd_abbrechen,
        }
        if cmd in handlers:
            await handlers[cmd](update, ctx)
        return

    session = sessions.get(chat_id, {})
    raw     = text.replace(",", ".")
    gid     = _active_group(chat_id)

    # ── Projekt anlegen: name
    if session.get("stage") == "project_add_name":
        name = text.strip()
        if not name:
            await _reply(update, ctx, "❌ Name darf nicht leer sein.")
            return
        if db.get_project_by_name(name):
            await _reply(update, ctx, "❌ Projekt mit diesem Namen existiert bereits.")
            return
        db.add_project(name, chat_id)
        sessions.pop(chat_id, None)
        await _reply(update, ctx,
            f"✅ Projekt *{name}* angelegt. Nutze /projekte zum Anzeigen.")
        return

    # ── Group creation: name
    if session.get("stage") == "group_create_name":
        name = text.strip()
        if not name:
            await _reply(update, ctx, "❌ Name darf nicht leer sein.")
            return
        session.update({"group_name": name, "stage": "group_create_person_name"})
        sessions[chat_id] = session
        await _reply(update, ctx,
            f"📂 Gruppe *{name}* wird erstellt.\n\n"
            "👤 Wie heisst du in dieser Gruppe? (Dein Anzeigename):")
        return

    # ── Group creation: person name
    if session.get("stage") == "group_create_person_name":
        person_name = text.strip()
        if not person_name:
            await _reply(update, ctx, "❌ Name darf nicht leer sein.")
            return
        group_name = session.get("group_name", "Gruppe")
        new_gid = db.create_group(group_name, chat_id)
        # Create person in group and link
        pid = db.add_person(person_name, new_gid)
        db.link_person_chat(pid, chat_id)
        db.add_person_to_group(pid, new_gid, chat_id)
        sessions.pop(chat_id, None)
        g = db.get_group(new_gid)
        invite = g["invite_code"] if g else "?"
        await _reply(update, ctx,
            f"✅ Gruppe *{group_name}* erstellt!\n\n"
            f"📋 Einladungscode: `{invite}`\n"
            f"👤 Dein Name: *{person_name}*\n\n"
            f"Teile den Code, damit andere beitreten koennen.",
            set_active=False)
        return

    # ── Group join: code
    if session.get("stage") == "group_join_code":
        code = text.strip()
        if not code:
            await _reply(update, ctx, "❌ Code darf nicht leer sein.")
            return
        # Verify code exists
        conn = db.get_conn()
        g = conn.execute("SELECT id, name FROM groups WHERE invite_code=?", (code,)).fetchone()
        conn.close()
        if not g:
            await _reply(update, ctx, "❌ Ungueltiger Einladungscode. Versuche es erneut:")
            return
        session.update({
            "invite_code": code,
            "join_group_name": g["name"],
            "stage": "group_join_person_name"
        })
        sessions[chat_id] = session
        await _reply(update, ctx,
            f"🔗 Gruppe gefunden: *{g['name']}*\n\n"
            "👤 Wie heisst du in dieser Gruppe? (Dein Anzeigename):")
        return

    # ── Group join: person name
    if session.get("stage") == "group_join_person_name":
        person_name = text.strip()
        if not person_name:
            await _reply(update, ctx, "❌ Name darf nicht leer sein.")
            return
        code = session.get("invite_code", "")
        joined_gid = db.join_group(code, chat_id, person_name)
        sessions.pop(chat_id, None)
        if joined_gid:
            gname = session.get("join_group_name", "Gruppe")
            await _reply(update, ctx,
                f"✅ Du bist *{gname}* beigetreten!\n"
                f"👤 Dein Name: *{person_name}*",
                set_active=False)
        else:
            await _reply(update, ctx, "❌ Beitritt fehlgeschlagen.", set_active=False)
        return

    # ── Person hinzufuegen (standalone)
    if session.get("stage") == "person_add_name":
        name = text.strip()
        if not name:
            await _reply(update, ctx, "❌ Name darf nicht leer sein.")
            return
        if gid is None:
            gid = await _ensure_group(update, ctx)
            if gid is None:
                return
        db.add_person(name, gid)
        sessions.pop(chat_id, None)
        await _reply(update, ctx, f"✅ *{name}* hinzugefuegt.", set_active=False)
        return

    # ── Manueller Posten: Beschreibung
    if session.get("stage") == "posten_desc":
        session.update({"posten_desc": text, "stage": "posten_amount"})
        sessions[chat_id] = session
        await _reply(update, ctx, f"📝 *{text}*\n\n💰 Betrag eingeben (z.B. `25.50` oder `1200 THB`, `100 EUR`, `12 USD`).\nOhne Angabe: Default {CURRENCY}.")
        return

    # ── Manueller Posten: Betrag (optional + Währung, z.B. "1200 THB")
    if session.get("stage") == "posten_amount":
        import re as _re
        m = _re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*([A-Za-z]{3})?\s*", text)
        if not m:
            await update.message.reply_text(
                "❌ Bitte Betrag eingeben — z.B. `25.50`, `1200 THB`, `100 EUR`:",
                parse_mode="Markdown")
            return
        try:
            amount = float(m.group(1).replace(",", "."))
            if amount <= 0:
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "❌ Ungültiger Betrag. Beispiele: `25.50`, `1200 THB`:",
                parse_mode="Markdown")
            return
        currency = (m.group(2) or CURRENCY).upper()
        desc = session.get("posten_desc", "Posten")
        receipt_id = db.save_receipt(
            store=desc, date="", total=amount, currency=currency,
            note="manuell", file_path="", payer_id=None, my_share=0.0,
            group_id=gid,
        )
        item_ids = db.save_items(receipt_id, [{"description": desc, "amount": amount, "quantity": 1}])
        session.update({
            "stage": "payer_select",
            "receipt_id": receipt_id,
            "items": [{"id": item_ids[0], "description": desc, "amount": amount, "quantity": 1}],
            "data": {"currency": currency, "total": amount},
        })
        sessions[chat_id] = session
        persons = db.get_persons(gid)
        kbd = [[InlineKeyboardButton("👤 Ich habe gezahlt", callback_data="payer:me")]]
        for p in persons:
            kbd.append([InlineKeyboardButton(f"💳 {p['name']} hat gezahlt",
                                             callback_data=f"payer:{p['id']}")])
        kbd.append([InlineKeyboardButton("➕ Person hinzufügen", callback_data="inline_add_person")])
        kbd.append([InlineKeyboardButton("❌ Abbrechen", callback_data="cancel")])
        await _reply(update, ctx,
            f"➕ *{desc}* – {currency} {amount:.2f}\n\n❓ *Wer hat gezahlt?*",
            inline=InlineKeyboardMarkup(kbd))
        return

    # ── Inline Person erstellen
    if session.get("stage") == "inline_add_person":
        name = text.strip()
        if not name:
            await _reply(update, ctx, "❌ Name darf nicht leer sein.")
            return
        if gid is None:
            gid = await _ensure_group(update, ctx)
            if gid is None:
                return
        db.add_person(name, gid)
        prev_stage = session.get("_pre_add_stage", "")
        session.pop("_pre_add_stage", None)
        session["stage"] = prev_stage
        sessions[chat_id] = session
        await _reply(update, ctx, f"✅ *{name}* hinzugefuegt.")
        await _rebuild_person_keyboard(ctx, chat_id, session, prev_stage)
        return

    if session.get("stage") == "geld_amount":
        try:
            amount = float(raw)
            if amount <= 0:
                raise ValueError
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



# ── Admin: User-Verwaltung ────────────────────────────────────────────────────

@_log_action("cmd_users")
async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Liste aller User (Admins + approved). Nur für Admins."""
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("🚫 Nur Admins.")
        return

    admins = list(ALLOWED_CHAT_IDS)
    approved = db.get_allowed_chats_full()
    pending = sorted(_pending_requests)

    lines = ["👥 *User-Verwaltung*\n"]
    lines.append(f"*👑 Admins* ({len(admins)})")
    for aid in admins:
        lines.append(f"• `{aid}`")
    lines.append("")

    lines.append(f"*✅ Approved* ({len(approved)})")
    if not approved:
        lines.append("_keine_")
    else:
        for u in approved:
            name = u.get("name") or "?"
            lines.append(f"• *{name}* — `{u['chat_id']}`")
    lines.append("")

    lines.append(f"*⏳ Pending* ({len(pending)})")
    if not pending:
        lines.append("_keine_")
    else:
        for pid in pending:
            lines.append(f"• `{pid}`")

    rows = []
    # pending: approve + deny per Tap
    for pid in pending[:5]:
        rows.append([
            InlineKeyboardButton(f"✅ Erlauben {pid}", callback_data=f"access_grant:{pid}:#{pid}"),
            InlineKeyboardButton(f"❌ Ablehnen {pid}", callback_data=f"access_deny:{pid}"),
        ])
    # approved: revoke per Tap
    for u in approved[:8]:
        name = (u.get("name") or str(u["chat_id"]))[:18]
        rows.append([InlineKeyboardButton(
            f"🚫 Sperren: {name}",
            callback_data=f"user_revoke:{u['chat_id']}"
        )])
    rows.append([InlineKeyboardButton("🔄 Aktualisieren", callback_data="show_users")])

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows) if rows else None
    )


@_log_action("cmd_revoke")
async def cmd_revoke(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """/revoke <chat_id> — User aus allowed_chats entfernen. Nur für Admins."""
    chat_id = update.effective_chat.id
    if not _is_admin(chat_id):
        await update.message.reply_text("🚫 Nur Admins.")
        return
    if not ctx.args:
        await update.message.reply_text("Nutzung: `/revoke <chat_id>`", parse_mode="Markdown")
        return
    try:
        target = int(ctx.args[0])
    except ValueError:
        await update.message.reply_text("Ungültige Chat-ID.")
        return
    if target in ALLOWED_CHAT_IDS:
        await update.message.reply_text(
            "⚠️ User ist Admin (in ALLOWED_CHAT_IDS) — manuell aus .env entfernen + Bot neu starten."
        )
        return
    db.remove_allowed_chat(target)
    _pending_requests.discard(target)
    await update.message.reply_text(f"🚫 User `{target}` gesperrt.", parse_mode="Markdown")
    try:
        await ctx.bot.send_message(target, "🚫 Dein Zugriff wurde widerrufen.")
    except Exception:
        pass



@_log_action("cmd_projekte")
async def cmd_projekte(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Projekt-Verwaltung — alle User können listen, jeder kann anlegen/archivieren."""
    if not _auth(update): return
    chat_id = update.effective_chat.id
    all_projects = db.get_projects(include_archived=True)
    active = [p for p in all_projects if not p["archived"]]
    archived = [p for p in all_projects if p["archived"]]

    lines = ["📁 *Projekte*\n"]
    lines.append(f"*Aktiv* ({len(active)})")
    if not active:
        lines.append("_keine_")
    else:
        for pj in active:
            total = db.get_project_total(pj["id"])
            lines.append(f"• *{pj['name']}* — {total:.2f}")
    lines.append("")
    if archived:
        lines.append(f"*Archiviert* ({len(archived)})")
        for pj in archived:
            lines.append(f"• {pj['name']}")
        lines.append("")

    rows = []
    for pj in active[:8]:
        rows.append([
            InlineKeyboardButton(f"📋 {pj['name']}", callback_data=f"project_detail:{pj['id']}"),
            InlineKeyboardButton("📦 Archivieren", callback_data=f"project_archive:{pj['id']}"),
            InlineKeyboardButton("🗑", callback_data=f"project_delete:{pj['id']}"),
        ])
    rows.append([InlineKeyboardButton("➕ Neues Projekt", callback_data="project_add")])
    rows.append([InlineKeyboardButton("🔄 Aktualisieren", callback_data="project_list")])

    await update.message.reply_text(
        "\n".join(lines), parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(rows)
    )


# ── Application ───────────────────────────────────────────────────────────────

async def error_handler(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    """Globaler Error-Handler – loggt full + sendet Fehlermeldungen an User + Admins."""
    import traceback
    tb = "".join(traceback.format_exception(type(ctx.error), ctx.error, ctx.error.__traceback__))
    chat_id = update.effective_chat.id if hasattr(update, "effective_chat") and update.effective_chat else "?"
    user_id = update.effective_user.id if hasattr(update, "effective_user") and update.effective_user else "?"
    log.error("UNHANDLED EXCEPTION chat=%s user=%s\nUpdate=%r\nTraceback:\n%s",
              chat_id, user_id, update, tb)
    if update and hasattr(update, "effective_chat") and update.effective_chat:
        try:
            await ctx.bot.send_message(
                update.effective_chat.id,
                f"❌ Fehler: {ctx.error}",
            )
        except Exception:
            pass
    # Forward error trace to all admins (truncated)
    excerpt = tb[-1500:] if len(tb) > 1500 else tb
    for admin in ALLOWED_CHAT_IDS:
        try:
            await ctx.bot.send_message(
                admin,
                f"⚠️ *Bot-Fehler* chat=`{chat_id}` user=`{user_id}`\n```\n{excerpt}\n```",
                parse_mode="Markdown"
            )
        except Exception:
            pass


def build_application() -> Application:
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start",      cmd_start))
    app.add_handler(CommandHandler("personen",   cmd_personen))
    app.add_handler(CommandHandler("person_add", cmd_person_add))
    app.add_handler(CommandHandler("person_del", cmd_person_del))
    app.add_handler(CommandHandler("saldo",      cmd_saldo))
    app.add_handler(CommandHandler("detail",     cmd_detail))
    app.add_handler(CommandHandler("verlauf",    cmd_verlauf))
    app.add_handler(CommandHandler("quittungen", cmd_quittungen))
    app.add_handler(CommandHandler("geld",       cmd_geld))
    app.add_handler(CommandHandler("posten",     cmd_posten))
    app.add_handler(CommandHandler("loeschen",   cmd_loeschen))
    app.add_handler(CommandHandler("abbrechen",  cmd_abbrechen))
    app.add_handler(CommandHandler("gruppe",     cmd_gruppe))
    app.add_handler(CommandHandler("projekte",   cmd_projekte))
    app.add_handler(CommandHandler("users",      cmd_users))
    app.add_handler(CommandHandler("revoke",     cmd_revoke))
    app.add_handler(MessageHandler(filters.PHOTO | filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    return app


async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",      "Hilfe & Uebersicht"),
        BotCommand("gruppe",     "📂 Gruppen verwalten"),
        BotCommand("geld",       "💸 Zahlung buchen"),
        BotCommand("posten",     "➕ Manueller Posten"),
        BotCommand("saldo",      "Aktuelle Salden"),
        BotCommand("detail",     "Kontoauszug einer Person"),
        BotCommand("verlauf",    "Letzte Zahlungen"),
        BotCommand("quittungen", "Letzte Quittungen"),
        BotCommand("personen",   "Alle Personen & Salden"),
        BotCommand("person_add", "Person hinzufuegen"),
        BotCommand("person_del", "Person entfernen"),
        BotCommand("loeschen",   "Letzten Eintrag rueckgaengig"),
        BotCommand("projekte",   "📁 Projekte verwalten"),
        BotCommand("abbrechen",  "Eingabe abbrechen"),
        BotCommand("users",      "👥 User-Verwaltung (Admin)"),
        BotCommand("revoke",     "🚫 User sperren (Admin)"),
    ])
