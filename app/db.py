import sqlite3
import os
from typing import Optional
from app.config import DATABASE_PATH, UPLOAD_PATH


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    os.makedirs(UPLOAD_PATH, exist_ok=True)
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS persons (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS receipts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            store       TEXT,
            date        TEXT,
            total       REAL,
            currency    TEXT DEFAULT 'CHF',
            note        TEXT,
            file_path   TEXT,
            -- NULL = ich habe gezahlt; gesetzt = diese Person hat gezahlt
            payer_id    INTEGER,
            -- Mein Anteil wenn jemand anderes gezahlt hat
            my_share    REAL DEFAULT 0,
            uploaded_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (payer_id) REFERENCES persons(id)
        );

        CREATE TABLE IF NOT EXISTS items (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            receipt_id  INTEGER NOT NULL,
            description TEXT NOT NULL,
            amount      REAL NOT NULL,
            quantity    REAL DEFAULT 1,
            FOREIGN KEY (receipt_id) REFERENCES receipts(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS item_assignments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id      INTEGER NOT NULL,
            person_id    INTEGER NOT NULL,
            share_amount REAL NOT NULL,
            FOREIGN KEY (item_id)   REFERENCES items(id)   ON DELETE CASCADE,
            FOREIGN KEY (person_id) REFERENCES persons(id) ON DELETE CASCADE
        );

        -- direction='received': Person hat mir Geld gegeben  (reduziert Schuld / begleicht)
        -- direction='paid':     Ich habe Person Geld gegeben (reduziert meine Schuld / begleicht)
        CREATE TABLE IF NOT EXISTS cash_transfers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id  INTEGER NOT NULL,
            amount     REAL NOT NULL,
            direction  TEXT NOT NULL CHECK(direction IN ('received','paid')),
            note       TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (person_id) REFERENCES persons(id) ON DELETE CASCADE
        );

        -- Migration handled below for existing DBs
        CREATE TABLE IF NOT EXISTS allowed_chats (
            chat_id     INTEGER PRIMARY KEY,
            name        TEXT,
            approved_by INTEGER,
            created_at  TEXT DEFAULT (datetime('now'))
        );
    """)
    # Migration: add payer_id / my_share to existing DB if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(receipts)").fetchall()]
    if "payer_id" not in cols:
        conn.execute("ALTER TABLE receipts ADD COLUMN payer_id INTEGER")
    if "my_share" not in cols:
        conn.execute("ALTER TABLE receipts ADD COLUMN my_share REAL DEFAULT 0")
    # Migration: add chat_id to persons
    p_cols = [r[1] for r in conn.execute("PRAGMA table_info(persons)").fetchall()]
    if "chat_id" not in p_cols:
        conn.execute("ALTER TABLE persons ADD COLUMN chat_id INTEGER")
    conn.commit()
    conn.close()


# ── Persons ───────────────────────────────────────────────────────────────────

def get_persons() -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("SELECT * FROM persons ORDER BY name").fetchall()
    conn.close()
    return rows


def get_person(person_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM persons WHERE id=?", (person_id,)).fetchone()
    conn.close()
    return row


def add_person(name: str) -> int:
    conn = get_conn()
    cur = conn.execute("INSERT OR IGNORE INTO persons (name) VALUES (?)", (name,))
    conn.commit()
    pid = cur.lastrowid or conn.execute(
        "SELECT id FROM persons WHERE name=?", (name,)
    ).fetchone()["id"]
    conn.close()
    return pid


def link_person_chat(person_id: int, chat_id: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE persons SET chat_id=? WHERE id=?", (chat_id, person_id))
    conn.commit()
    conn.close()


def get_person_by_chat(chat_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM persons WHERE chat_id=?", (chat_id,)).fetchone()
    conn.close()
    return row


def delete_person(person_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM persons WHERE id=?", (person_id,))
    conn.commit()
    conn.close()


# ── Receipts ──────────────────────────────────────────────────────────────────

def save_receipt(store: str, date: str, total: float, currency: str,
                 note: str, file_path: str,
                 payer_id: Optional[int] = None,
                 my_share: float = 0.0) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO receipts "
        "(store, date, total, currency, note, file_path, payer_id, my_share) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (store, date, total, currency, note, file_path, payer_id, my_share)
    )
    conn.commit()
    rid = cur.lastrowid
    conn.close()
    return rid


def update_receipt_payer(receipt_id: int, payer_id: Optional[int],
                         my_share: float) -> None:
    conn = get_conn()
    conn.execute(
        "UPDATE receipts SET payer_id=?, my_share=? WHERE id=?",
        (payer_id, my_share, receipt_id)
    )
    conn.commit()
    conn.close()


def save_items(receipt_id: int, items: list[dict]) -> list[int]:
    conn = get_conn()
    ids = []
    for item in items:
        cur = conn.execute(
            "INSERT INTO items (receipt_id, description, amount, quantity) VALUES (?,?,?,?)",
            (receipt_id, item["description"], item["amount"], item.get("quantity", 1))
        )
        ids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    return ids


def assign_item(item_id: int, person_id: int, share_amount: float) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM item_assignments WHERE item_id=?", (item_id,))
    conn.execute(
        "INSERT INTO item_assignments (item_id, person_id, share_amount) VALUES (?,?,?)",
        (item_id, person_id, share_amount)
    )
    conn.commit()
    conn.close()


def assign_item_split(item_id: int, person_ids: list[int], share_amount: float) -> None:
    each = round(share_amount / len(person_ids), 4)
    conn = get_conn()
    conn.execute("DELETE FROM item_assignments WHERE item_id=?", (item_id,))
    for pid in person_ids:
        conn.execute(
            "INSERT INTO item_assignments (item_id, person_id, share_amount) VALUES (?,?,?)",
            (item_id, pid, each)
        )
    conn.commit()
    conn.close()


def assign_all_split(item_ids: list[int], amounts: list[float],
                     person_ids: list[int]) -> None:
    conn = get_conn()
    for item_id, amount in zip(item_ids, amounts):
        each = round(amount / len(person_ids), 4)
        conn.execute("DELETE FROM item_assignments WHERE item_id=?", (item_id,))
        for pid in person_ids:
            conn.execute(
                "INSERT INTO item_assignments (item_id, person_id, share_amount) VALUES (?,?,?)",
                (item_id, pid, each)
            )
    conn.commit()
    conn.close()


def get_receipts(limit: int = 50) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT r.*, p.name as payer_name
           FROM receipts r
           LEFT JOIN persons p ON p.id = r.payer_id
           ORDER BY r.uploaded_at DESC LIMIT ?""",
        (limit,)
    ).fetchall()
    conn.close()
    return rows


def get_items_for_receipt(receipt_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT i.*,
                  GROUP_CONCAT(p.name, ', ') as person_names,
                  SUM(ia.share_amount) as total_assigned
           FROM items i
           LEFT JOIN item_assignments ia ON ia.item_id = i.id
           LEFT JOIN persons p ON p.id = ia.person_id
           WHERE i.receipt_id = ?
           GROUP BY i.id ORDER BY i.id""",
        (receipt_id,)
    ).fetchall()
    conn.close()
    return rows


def get_total_assigned(receipt_id: int) -> float:
    """Summe aller Zuweisungen für eine Quittung."""
    conn = get_conn()
    row = conn.execute("""
        SELECT COALESCE(SUM(ia.share_amount), 0) as total
        FROM item_assignments ia
        JOIN items i ON i.id = ia.item_id
        WHERE i.receipt_id = ?
    """, (receipt_id,)).fetchone()
    conn.close()
    return row["total"]


def delete_receipt(receipt_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM receipts WHERE id=?", (receipt_id,))
    conn.commit()
    conn.close()


# ── Cash Transfers ────────────────────────────────────────────────────────────

def add_cash_transfer(person_id: int, amount: float,
                      direction: str, note: str = "") -> int:
    assert direction in ("received", "paid")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO cash_transfers (person_id, amount, direction, note) VALUES (?,?,?,?)",
        (person_id, amount, direction, note)
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def get_cash_transfers(person_id: Optional[int] = None,
                       limit: int = 30) -> list[sqlite3.Row]:
    conn = get_conn()
    if person_id:
        rows = conn.execute(
            """SELECT ct.*, p.name FROM cash_transfers ct
               JOIN persons p ON p.id = ct.person_id
               WHERE ct.person_id=? ORDER BY ct.created_at DESC LIMIT ?""",
            (person_id, limit)
        ).fetchall()
    else:
        rows = conn.execute(
            """SELECT ct.*, p.name FROM cash_transfers ct
               JOIN persons p ON p.id = ct.person_id
               ORDER BY ct.created_at DESC LIMIT ?""",
            (limit,)
        ).fetchall()
    conn.close()
    return rows


def delete_cash_transfer(transfer_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM cash_transfers WHERE id=?", (transfer_id,))
    conn.commit()
    conn.close()


# ── Balances ──────────────────────────────────────────────────────────────────

def get_balances(my_person_id: Optional[int] = None) -> list[dict]:
    """
    Saldo-Logik (ich-zentrisch):

    balance > 0 → Person schuldet MIR Geld
    balance < 0 → ICH schulde Person Geld
    balance = 0 → quitt

    Wenn my_person_id gesetzt: "ich" ist eine Person in der DB.
    Zuweisungen an mich bei fremdem Zahler → ich schulde dem Zahler.
    """
    conn = get_conn()

    # Was Personen mir schulden (ich habe gezahlt ODER ich bin der Zahler als Person)
    if my_person_id:
        # Ich = Person: was andere mir schulden = Zuweisungen an andere
        # bei Quittungen die ICH gezahlt habe (payer_id = ich ODER payer_id IS NULL)
        owed_to_me = conn.execute("""
            SELECT ia.person_id, SUM(ia.share_amount) as total
            FROM item_assignments ia
            JOIN items i ON i.id = ia.item_id
            JOIN receipts r ON r.id = i.receipt_id
            WHERE (r.payer_id = ? OR r.payer_id IS NULL)
              AND ia.person_id != ?
            GROUP BY ia.person_id
        """, (my_person_id, my_person_id)).fetchall()
    else:
        owed_to_me = conn.execute("""
            SELECT ia.person_id, SUM(ia.share_amount) as total
            FROM item_assignments ia
            JOIN items i ON i.id = ia.item_id
            JOIN receipts r ON r.id = i.receipt_id
            WHERE r.payer_id IS NULL
            GROUP BY ia.person_id
        """).fetchall()

    # Was ich Personen schulde
    if my_person_id:
        # Meine Zuweisungen bei Quittungen die ANDERE gezahlt haben
        # (payer_id ist gesetzt UND ist nicht ich UND ist nicht NULL)
        i_owe = conn.execute("""
            SELECT r.payer_id, SUM(ia.share_amount) as total
            FROM item_assignments ia
            JOIN items i ON i.id = ia.item_id
            JOIN receipts r ON r.id = i.receipt_id
            WHERE ia.person_id = ?
              AND r.payer_id IS NOT NULL
              AND r.payer_id != ?
            GROUP BY r.payer_id
        """, (my_person_id, my_person_id)).fetchall()
    else:
        i_owe = conn.execute("""
            SELECT payer_id, SUM(my_share) as total
            FROM receipts
            WHERE payer_id IS NOT NULL AND my_share > 0
            GROUP BY payer_id
        """).fetchall()

    # Cash transfers
    transfers = conn.execute("""
        SELECT person_id, direction, SUM(amount) as total
        FROM cash_transfers
        GROUP BY person_id, direction
    """).fetchall()

    persons = conn.execute("SELECT * FROM persons ORDER BY name").fetchall()
    conn.close()

    owed_map  = {r["person_id"]: r["total"] for r in owed_to_me}
    i_owe_map = {r["payer_id"]:  r["total"] for r in i_owe}
    tf: dict[int, dict] = {}
    for t in transfers:
        pid = t["person_id"]
        tf.setdefault(pid, {"received": 0.0, "paid": 0.0})
        tf[pid][t["direction"]] += t["total"]

    result = []
    for p in persons:
        pid = p["id"]
        if pid == my_person_id:
            continue  # Mich selbst nicht anzeigen
        t = tf.get(pid, {"received": 0.0, "paid": 0.0})
        owed   = owed_map.get(pid, 0.0)   # P schuldet mir
        i_owe_ = i_owe_map.get(pid, 0.0)  # Ich schulde P

        balance = owed - i_owe_ - t["received"] + t["paid"]
        result.append({
            "id":       pid,
            "name":     p["name"],
            "owed_me":  round(owed, 2),
            "i_owe":    round(i_owe_, 2),
            "received": round(t["received"], 2),
            "paid":     round(t["paid"], 2),
            "balance":  round(balance, 2),
        })

    return result


def get_person_history(person_id: int) -> dict:
    conn = get_conn()
    person = conn.execute("SELECT * FROM persons WHERE id=?", (person_id,)).fetchone()

    # Items assigned to person (they owe me)
    items_owe_me = conn.execute("""
        SELECT i.description, ia.share_amount, r.store, r.date, r.currency
        FROM item_assignments ia
        JOIN items i ON i.id = ia.item_id
        JOIN receipts r ON r.id = i.receipt_id
        WHERE ia.person_id=? AND r.payer_id IS NULL
        ORDER BY r.uploaded_at DESC
    """, (person_id,)).fetchall()

    # Receipts person paid where I have a share (I owe them)
    receipts_they_paid = conn.execute("""
        SELECT store, date, total, my_share, currency
        FROM receipts
        WHERE payer_id=? AND my_share > 0
        ORDER BY uploaded_at DESC
    """, (person_id,)).fetchall()

    # Cash transfers
    transfers = conn.execute(
        "SELECT * FROM cash_transfers WHERE person_id=? ORDER BY created_at DESC",
        (person_id,)
    ).fetchall()

    conn.close()
    return {
        "person":            dict(person) if person else {},
        "items_owe_me":      [dict(i) for i in items_owe_me],
        "receipts_they_paid": [dict(r) for r in receipts_they_paid],
        "transfers":         [dict(t) for t in transfers],
    }


# ── Allowed Chats ────────────────────────────────────────────────────────────

def get_allowed_chats() -> list[int]:
    conn = get_conn()
    rows = conn.execute("SELECT chat_id FROM allowed_chats").fetchall()
    conn.close()
    return [r["chat_id"] for r in rows]


def add_allowed_chat(chat_id: int, name: str, approved_by: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR IGNORE INTO allowed_chats (chat_id, name, approved_by) VALUES (?,?,?)",
        (chat_id, name, approved_by)
    )
    conn.commit()
    conn.close()


def remove_allowed_chat(chat_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM allowed_chats WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()
