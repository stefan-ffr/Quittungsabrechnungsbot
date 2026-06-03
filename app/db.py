import sqlite3
import os
import string
import random
from typing import Optional
from app.config import DATABASE_PATH, UPLOAD_PATH


def get_conn() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _generate_invite_code(length: int = 8) -> str:
    chars = string.ascii_letters + string.digits
    return "".join(random.choices(chars, k=length))


def init_db() -> None:
    os.makedirs(UPLOAD_PATH, exist_ok=True)
    conn = get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS persons (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            chat_id    INTEGER,
            group_id   INTEGER,
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
            payer_id    INTEGER,
            my_share    REAL DEFAULT 0,
            group_id    INTEGER,
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

        CREATE TABLE IF NOT EXISTS cash_transfers (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            person_id  INTEGER NOT NULL,
            amount     REAL NOT NULL,
            direction  TEXT NOT NULL CHECK(direction IN ('received','paid')),
            note       TEXT,
            group_id   INTEGER,
            created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (person_id) REFERENCES persons(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS projects (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            archived    INTEGER NOT NULL DEFAULT 0,
            created_by  INTEGER,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS item_project_assignments (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            item_id      INTEGER NOT NULL,
            project_id   INTEGER NOT NULL,
            share_amount REAL NOT NULL,
            UNIQUE(item_id, project_id),
            FOREIGN KEY (item_id)    REFERENCES items(id)    ON DELETE CASCADE,
            FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_money_manager (
            chat_id    INTEGER PRIMARY KEY,
            url        TEXT NOT NULL,
            api_key    TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS allowed_chats (
            chat_id     INTEGER PRIMARY KEY,
            name        TEXT,
            approved_by INTEGER,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS groups (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL,
            invite_code TEXT UNIQUE,
            created_by  INTEGER,
            created_at  TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS group_members (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            group_id  INTEGER NOT NULL,
            chat_id   INTEGER NOT NULL,
            person_id INTEGER,
            role      TEXT DEFAULT 'member',
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE,
            FOREIGN KEY (person_id) REFERENCES persons(id),
            UNIQUE(group_id, chat_id)
        );

        CREATE TABLE IF NOT EXISTS active_groups (
            chat_id   INTEGER PRIMARY KEY,
            group_id  INTEGER NOT NULL,
            FOREIGN KEY (group_id) REFERENCES groups(id) ON DELETE CASCADE
        );
    """)

    # ── Migrations for existing DBs ──────────────────────────────────────────

    # Migration: add payer_id / my_share to existing DB if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(receipts)").fetchall()]
    if "payer_id" not in cols:
        conn.execute("ALTER TABLE receipts ADD COLUMN payer_id INTEGER")
    if "my_share" not in cols:
        conn.execute("ALTER TABLE receipts ADD COLUMN my_share REAL DEFAULT 0")
    if "group_id" not in cols:
        conn.execute("ALTER TABLE receipts ADD COLUMN group_id INTEGER")

    # Migration: add columns to persons
    p_cols = [r[1] for r in conn.execute("PRAGMA table_info(persons)").fetchall()]
    if "chat_id" not in p_cols:
        conn.execute("ALTER TABLE persons ADD COLUMN chat_id INTEGER")
    if "group_id" not in p_cols:
        conn.execute("ALTER TABLE persons ADD COLUMN group_id INTEGER")

    # Migration: add group_id to cash_transfers
    ct_cols = [r[1] for r in conn.execute("PRAGMA table_info(cash_transfers)").fetchall()]
    if "group_id" not in ct_cols:
        conn.execute("ALTER TABLE cash_transfers ADD COLUMN group_id INTEGER")

    # Remove old UNIQUE constraint on persons.name by recreating the table.
    # Check if the old constraint still exists (autoindex = built-in UNIQUE).
    old_indexes = conn.execute("PRAGMA index_list(persons)").fetchall()
    has_old_unique = any("autoindex" in (r["name"] or "") for r in old_indexes)
    if has_old_unique:
        conn.executescript("""
            PRAGMA foreign_keys=OFF;
            CREATE TABLE IF NOT EXISTS persons_new (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                name       TEXT NOT NULL,
                chat_id    INTEGER,
                group_id   INTEGER,
                created_at TEXT DEFAULT (datetime('now'))
            );
            INSERT OR IGNORE INTO persons_new SELECT id, name, chat_id, group_id, created_at FROM persons;
            DROP TABLE persons;
            ALTER TABLE persons_new RENAME TO persons;
            CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_name_group ON persons(name, group_id);
            PRAGMA foreign_keys=ON;
        """)
    else:
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_persons_name_group "
            "ON persons(name, group_id)")

    # ── Auto-migration: assign existing data to a default group ──────────
    # Check if there are any persons WITHOUT a group_id
    orphan = conn.execute(
        "SELECT COUNT(*) as cnt FROM persons WHERE group_id IS NULL"
    ).fetchone()["cnt"]

    if orphan > 0:
        # Create default group if it doesn't exist
        existing_default = conn.execute(
            "SELECT id FROM groups WHERE name='Standard' AND created_by IS NULL"
        ).fetchone()
        if existing_default:
            default_gid = existing_default["id"]
        else:
            code = _generate_invite_code()
            cur = conn.execute(
                "INSERT INTO groups (name, invite_code, created_by) VALUES (?,?,?)",
                ("Standard", code, None))
            default_gid = cur.lastrowid

        # Assign all orphan persons to default group
        conn.execute(
            "UPDATE persons SET group_id=? WHERE group_id IS NULL",
            (default_gid,))
        # Assign all orphan receipts
        conn.execute(
            "UPDATE receipts SET group_id=? WHERE group_id IS NULL",
            (default_gid,))
        # Assign all orphan cash_transfers
        conn.execute(
            "UPDATE cash_transfers SET group_id=? WHERE group_id IS NULL",
            (default_gid,))

        # Create group_members entries for persons that have a chat_id
        linked = conn.execute(
            "SELECT id, chat_id FROM persons WHERE chat_id IS NOT NULL AND group_id=?",
            (default_gid,)
        ).fetchall()
        for p in linked:
            conn.execute(
                "INSERT OR IGNORE INTO group_members (group_id, chat_id, person_id, role) "
                "VALUES (?,?,?,?)",
                (default_gid, p["chat_id"], p["id"], "admin"))
            # Set active group
            conn.execute(
                "INSERT OR IGNORE INTO active_groups (chat_id, group_id) VALUES (?,?)",
                (p["chat_id"], default_gid))

    conn.commit()
    conn.close()


# ── Groups ───────────────────────────────────────────────────────────────────

def create_group(name: str, chat_id: int) -> int:
    conn = get_conn()
    code = _generate_invite_code()
    cur = conn.execute(
        "INSERT INTO groups (name, invite_code, created_by) VALUES (?,?,?)",
        (name, code, chat_id))
    gid = cur.lastrowid
    conn.execute(
        "INSERT INTO group_members (group_id, chat_id, role) VALUES (?,?,?)",
        (gid, chat_id, "admin"))
    # Set as active group
    conn.execute(
        "INSERT OR REPLACE INTO active_groups (chat_id, group_id) VALUES (?,?)",
        (chat_id, gid))
    conn.commit()
    conn.close()
    return gid


def get_groups_for_chat(chat_id: int) -> list[sqlite3.Row]:
    conn = get_conn()
    rows = conn.execute("""
        SELECT g.*, gm.role, gm.person_id
        FROM groups g
        JOIN group_members gm ON gm.group_id = g.id
        WHERE gm.chat_id = ?
        ORDER BY g.name
    """, (chat_id,)).fetchall()
    conn.close()
    return rows


def get_group(group_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
    conn.close()
    return row


def join_group(invite_code: str, chat_id: int, person_name: str) -> Optional[int]:
    """Join a group via invite code. Returns group_id or None if code invalid."""
    conn = get_conn()
    g = conn.execute(
        "SELECT id FROM groups WHERE invite_code=?", (invite_code,)
    ).fetchone()
    if not g:
        conn.close()
        return None
    gid = g["id"]
    # Check if already member
    existing = conn.execute(
        "SELECT id FROM group_members WHERE group_id=? AND chat_id=?",
        (gid, chat_id)).fetchone()
    if existing:
        conn.close()
        return gid
    # Create person in group
    cur = conn.execute(
        "INSERT INTO persons (name, chat_id, group_id) VALUES (?,?,?)",
        (person_name, chat_id, gid))
    pid = cur.lastrowid
    conn.execute(
        "INSERT INTO group_members (group_id, chat_id, person_id, role) VALUES (?,?,?,?)",
        (gid, chat_id, pid, "member"))
    conn.execute(
        "INSERT OR REPLACE INTO active_groups (chat_id, group_id) VALUES (?,?)",
        (chat_id, gid))
    conn.commit()
    conn.close()
    return gid


def join_group_with_person(invite_code: str, chat_id: int,
                           person_id: int) -> Optional[int]:
    """Join a group via invite code and link to an existing person.
    Returns group_id or None if code invalid."""
    conn = get_conn()
    g = conn.execute(
        "SELECT id FROM groups WHERE invite_code=?", (invite_code,)
    ).fetchone()
    if not g:
        conn.close()
        return None
    gid = g["id"]
    # Check if already member
    existing = conn.execute(
        "SELECT id FROM group_members WHERE group_id=? AND chat_id=?",
        (gid, chat_id)).fetchone()
    if existing:
        conn.close()
        return gid
    # Link person to this chat
    conn.execute("UPDATE persons SET chat_id=? WHERE id=? AND group_id=?",
                 (chat_id, person_id, gid))
    conn.execute(
        "INSERT INTO group_members (group_id, chat_id, person_id, role) VALUES (?,?,?,?)",
        (gid, chat_id, person_id, "member"))
    conn.execute(
        "INSERT OR REPLACE INTO active_groups (chat_id, group_id) VALUES (?,?)",
        (chat_id, gid))
    conn.commit()
    conn.close()
    return gid


def get_active_group(chat_id: int) -> Optional[int]:
    conn = get_conn()
    row = conn.execute(
        "SELECT group_id FROM active_groups WHERE chat_id=?", (chat_id,)
    ).fetchone()
    conn.close()
    return row["group_id"] if row else None


def set_active_group(chat_id: int, group_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO active_groups (chat_id, group_id) VALUES (?,?)",
        (chat_id, group_id))
    conn.commit()
    conn.close()


def leave_group(chat_id: int, group_id: int) -> None:
    conn = get_conn()
    conn.execute(
        "DELETE FROM group_members WHERE group_id=? AND chat_id=?",
        (group_id, chat_id))
    # If active group was this one, clear it or switch
    active = conn.execute(
        "SELECT group_id FROM active_groups WHERE chat_id=?", (chat_id,)
    ).fetchone()
    if active and active["group_id"] == group_id:
        # Switch to another group if available
        other = conn.execute(
            "SELECT group_id FROM group_members WHERE chat_id=? LIMIT 1",
            (chat_id,)).fetchone()
        if other:
            conn.execute(
                "UPDATE active_groups SET group_id=? WHERE chat_id=?",
                (other["group_id"], chat_id))
        else:
            conn.execute("DELETE FROM active_groups WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def add_person_to_group(person_id: int, group_id: int, chat_id: int) -> None:
    """Link a person to a group membership entry."""
    conn = get_conn()
    conn.execute(
        "UPDATE group_members SET person_id=? WHERE group_id=? AND chat_id=?",
        (person_id, group_id, chat_id))
    conn.commit()
    conn.close()


# ── Persons ───────────────────────────────────────────────────────────────────

def get_persons(group_id: Optional[int] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    if group_id is not None:
        rows = conn.execute(
            "SELECT * FROM persons WHERE group_id=? ORDER BY name",
            (group_id,)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM persons ORDER BY name").fetchall()
    conn.close()
    return rows


def get_person(person_id: int) -> Optional[sqlite3.Row]:
    conn = get_conn()
    row = conn.execute("SELECT * FROM persons WHERE id=?", (person_id,)).fetchone()
    conn.close()
    return row


def add_person(name: str, group_id: Optional[int] = None) -> int:
    conn = get_conn()
    if group_id is not None:
        # Check if person with same name already exists in this group
        existing = conn.execute(
            "SELECT id FROM persons WHERE name=? AND group_id=?",
            (name, group_id)).fetchone()
        if existing:
            conn.close()
            return existing["id"]
        cur = conn.execute(
            "INSERT INTO persons (name, group_id) VALUES (?,?)",
            (name, group_id))
    else:
        cur = conn.execute(
            "INSERT OR IGNORE INTO persons (name) VALUES (?)", (name,))
        if cur.lastrowid == 0:
            pid = conn.execute(
                "SELECT id FROM persons WHERE name=?", (name,)
            ).fetchone()["id"]
            conn.close()
            return pid
    conn.commit()
    pid = cur.lastrowid or conn.execute(
        "SELECT id FROM persons WHERE name=? AND group_id=?", (name, group_id)
    ).fetchone()["id"]
    conn.close()
    return pid


def link_person_chat(person_id: int, chat_id: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE persons SET chat_id=? WHERE id=?", (chat_id, person_id))
    conn.commit()
    conn.close()


def get_person_by_chat(chat_id: int, group_id: Optional[int] = None) -> Optional[sqlite3.Row]:
    conn = get_conn()
    if group_id is not None:
        row = conn.execute(
            "SELECT * FROM persons WHERE chat_id=? AND group_id=?",
            (chat_id, group_id)).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM persons WHERE chat_id=?", (chat_id,)).fetchone()
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
                 my_share: float = 0.0,
                 group_id: Optional[int] = None) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO receipts "
        "(store, date, total, currency, note, file_path, payer_id, my_share, group_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (store, date, total, currency, note, file_path, payer_id, my_share, group_id)
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


def get_receipts(limit: int = 50, group_id: Optional[int] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    if group_id is not None:
        rows = conn.execute(
            """SELECT r.*, p.name as payer_name
               FROM receipts r
               LEFT JOIN persons p ON p.id = r.payer_id
               WHERE r.group_id=?
               ORDER BY r.uploaded_at DESC LIMIT ?""",
            (group_id, limit)
        ).fetchall()
    else:
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
    """Summe aller Zuweisungen fuer eine Quittung."""
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
                      direction: str, note: str = "",
                      group_id: Optional[int] = None) -> int:
    assert direction in ("received", "paid")
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO cash_transfers (person_id, amount, direction, note, group_id) "
        "VALUES (?,?,?,?,?)",
        (person_id, amount, direction, note, group_id)
    )
    conn.commit()
    tid = cur.lastrowid
    conn.close()
    return tid


def get_cash_transfers(person_id: Optional[int] = None,
                       limit: int = 30,
                       group_id: Optional[int] = None) -> list[sqlite3.Row]:
    conn = get_conn()
    if person_id:
        if group_id is not None:
            rows = conn.execute(
                """SELECT ct.*, p.name FROM cash_transfers ct
                   JOIN persons p ON p.id = ct.person_id
                   WHERE ct.person_id=? AND ct.group_id=?
                   ORDER BY ct.created_at DESC LIMIT ?""",
                (person_id, group_id, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT ct.*, p.name FROM cash_transfers ct
                   JOIN persons p ON p.id = ct.person_id
                   WHERE ct.person_id=? ORDER BY ct.created_at DESC LIMIT ?""",
                (person_id, limit)
            ).fetchall()
    else:
        if group_id is not None:
            rows = conn.execute(
                """SELECT ct.*, p.name FROM cash_transfers ct
                   JOIN persons p ON p.id = ct.person_id
                   WHERE ct.group_id=?
                   ORDER BY ct.created_at DESC LIMIT ?""",
                (group_id, limit)
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



def get_group_currency(group_id: int) -> str:
    """Häufigste Währung der Quittungen in einer Gruppe (Default CHF)."""
    conn = get_conn()
    r = conn.execute(
        """SELECT currency, COUNT(*) AS n FROM receipts
            WHERE group_id=? GROUP BY currency ORDER BY n DESC LIMIT 1""",
        (group_id,)).fetchone()
    conn.close()
    return r["currency"] if r and r["currency"] else "CHF"


def get_group_currencies(group_id: int) -> list[str]:
    """Alle Währungen die in einer Gruppe vorkommen."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT DISTINCT currency FROM receipts WHERE group_id=? AND currency IS NOT NULL ORDER BY currency",
        (group_id,)).fetchall()
    conn.close()
    return [r["currency"] for r in rows]


# ── Balances ──────────────────────────────────────────────────────────────────

def get_balances(my_person_id: Optional[int] = None,
                 group_id: Optional[int] = None) -> list[dict]:
    """
    Saldo-Logik (ich-zentrisch):

    balance > 0 -> Person schuldet MIR Geld
    balance < 0 -> ICH schulde Person Geld
    balance = 0 -> quitt

    Wenn my_person_id gesetzt: "ich" ist eine Person in der DB.
    Zuweisungen an mich bei fremdem Zahler -> ich schulde dem Zahler.
    """
    conn = get_conn()
    group_filter = "AND r.group_id = ?" if group_id is not None else ""
    group_params: tuple = (group_id,) if group_id is not None else ()

    # Was Personen mir schulden (ich habe gezahlt ODER ich bin der Zahler als Person)
    if my_person_id:
        owed_to_me = conn.execute(f"""
            SELECT ia.person_id, SUM(ia.share_amount) as total
            FROM item_assignments ia
            JOIN items i ON i.id = ia.item_id
            JOIN receipts r ON r.id = i.receipt_id
            WHERE (r.payer_id = ? OR r.payer_id IS NULL)
              AND ia.person_id != ?
              {group_filter}
            GROUP BY ia.person_id
        """, (my_person_id, my_person_id) + group_params).fetchall()
    else:
        owed_to_me = conn.execute(f"""
            SELECT ia.person_id, SUM(ia.share_amount) as total
            FROM item_assignments ia
            JOIN items i ON i.id = ia.item_id
            JOIN receipts r ON r.id = i.receipt_id
            WHERE r.payer_id IS NULL
              {group_filter}
            GROUP BY ia.person_id
        """, group_params).fetchall()

    # Was ich Personen schulde
    if my_person_id:
        i_owe = conn.execute(f"""
            SELECT r.payer_id, SUM(ia.share_amount) as total
            FROM item_assignments ia
            JOIN items i ON i.id = ia.item_id
            JOIN receipts r ON r.id = i.receipt_id
            WHERE ia.person_id = ?
              AND r.payer_id IS NOT NULL
              AND r.payer_id != ?
              {group_filter}
            GROUP BY r.payer_id
        """, (my_person_id, my_person_id) + group_params).fetchall()
    else:
        i_owe = conn.execute(f"""
            SELECT payer_id, SUM(my_share) as total
            FROM receipts
            WHERE payer_id IS NOT NULL AND my_share > 0
              {group_filter.replace('r.group_id', 'group_id')}
            GROUP BY payer_id
        """, group_params).fetchall()

    # Cash transfers
    ct_filter = "WHERE ct.group_id = ?" if group_id is not None else ""
    transfers = conn.execute(f"""
        SELECT ct.person_id, ct.direction, SUM(ct.amount) as total
        FROM cash_transfers ct
        {ct_filter}
        GROUP BY ct.person_id, ct.direction
    """, group_params).fetchall()

    if group_id is not None:
        persons = conn.execute(
            "SELECT * FROM persons WHERE group_id=? ORDER BY name",
            (group_id,)).fetchall()
    else:
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


def get_allowed_chats_full() -> list[dict]:
    """Returns all approved chats with name + approved_by + created_at."""
    conn = get_conn()
    rows = conn.execute(
        "SELECT chat_id, name, approved_by, created_at "
        "FROM allowed_chats ORDER BY created_at"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


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



# ── Projects (global, group-overarching) ─────────────────────────────────────

def add_project(name: str, created_by: int) -> int:
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO projects (name, created_by) VALUES (?,?)",
        (name.strip(), created_by))
    pid = cur.lastrowid
    conn.commit()
    conn.close()
    return pid


def get_projects(include_archived: bool = False) -> list[dict]:
    conn = get_conn()
    if include_archived:
        rows = conn.execute("SELECT * FROM projects ORDER BY archived, name").fetchall()
    else:
        rows = conn.execute("SELECT * FROM projects WHERE archived=0 ORDER BY name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_project(project_id: int) -> Optional[dict]:
    conn = get_conn()
    r = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    conn.close()
    return dict(r) if r else None


def get_project_by_name(name: str) -> Optional[dict]:
    conn = get_conn()
    r = conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
    conn.close()
    return dict(r) if r else None


def archive_project(project_id: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE projects SET archived=1 WHERE id=?", (project_id,))
    conn.commit()
    conn.close()


def unarchive_project(project_id: int) -> None:
    conn = get_conn()
    conn.execute("UPDATE projects SET archived=0 WHERE id=?", (project_id,))
    conn.commit()
    conn.close()


def delete_project(project_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM projects WHERE id=?", (project_id,))
    conn.commit()
    conn.close()


def assign_item_to_project(item_id: int, project_id: int, share_amount: float) -> None:
    """Weist ein Item einem Projekt zu (zusätzlich oder anstatt einer Person)."""
    conn = get_conn()
    conn.execute("DELETE FROM item_project_assignments WHERE item_id=?", (item_id,))
    conn.execute(
        "INSERT INTO item_project_assignments (item_id, project_id, share_amount) VALUES (?,?,?)",
        (item_id, project_id, share_amount))
    conn.commit()
    conn.close()


def get_project_total(project_id: int) -> float:
    conn = get_conn()
    r = conn.execute(
        "SELECT COALESCE(SUM(share_amount), 0) AS total "
        "FROM item_project_assignments WHERE project_id=?",
        (project_id,)).fetchone()
    conn.close()
    return float(r["total"])


def get_project_items(project_id: int, limit: int = 100) -> list[dict]:
    conn = get_conn()
    rows = conn.execute(
        """SELECT i.description, i.quantity, ipa.share_amount, r.store, r.date,
                  r.currency, g.name AS group_name
             FROM item_project_assignments ipa
             JOIN items i      ON i.id = ipa.item_id
             JOIN receipts r   ON r.id = i.receipt_id
             LEFT JOIN groups g ON g.id = r.group_id
            WHERE ipa.project_id=?
          ORDER BY r.date DESC, i.id DESC
            LIMIT ?""", (project_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]




# ── per-user Money-Manager-Config ────────────────────────────────────────────

def set_money_manager(chat_id: int, url: str, api_key: str) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO user_money_manager(chat_id, url, api_key) VALUES(?,?,?) "
        "ON CONFLICT(chat_id) DO UPDATE SET url=excluded.url, api_key=excluded.api_key",
        (chat_id, url.strip().rstrip("/"), api_key.strip()))
    conn.commit()
    conn.close()


def get_money_manager(chat_id: int) -> Optional[dict]:
    conn = get_conn()
    r = conn.execute(
        "SELECT url, api_key FROM user_money_manager WHERE chat_id=?",
        (chat_id,)).fetchone()
    conn.close()
    return dict(r) if r else None


def delete_money_manager(chat_id: int) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM user_money_manager WHERE chat_id=?", (chat_id,))
    conn.commit()
    conn.close()


def ensure_user_has_group(chat_id: int, default_name: Optional[str] = None) -> Optional[int]:
    """
    Ensure existing users (from ALLOWED_CHAT_IDS) have at least one group.
    If they have a person entry but no group membership, auto-create 'Standard'
    group and migrate them. If default_name given and no person exists yet,
    a person with that name is created + linked. Returns group_id or None.
    """
    conn = get_conn()
    # Check if user already has groups
    membership = conn.execute(
        "SELECT group_id FROM group_members WHERE chat_id=?", (chat_id,)
    ).fetchone()
    if membership:
        conn.close()
        return None  # Already has a group

    # Check if they have a person entry (legacy user)
    person = conn.execute(
        "SELECT id, group_id FROM persons WHERE chat_id=?", (chat_id,)
    ).fetchone()

    if person and person["group_id"]:
        # Person exists with group_id but no group_member entry - fix it
        gid = person["group_id"]
        conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, chat_id, person_id, role) "
            "VALUES (?,?,?,?)",
            (gid, chat_id, person["id"], "admin"))
        conn.execute(
            "INSERT OR REPLACE INTO active_groups (chat_id, group_id) VALUES (?,?)",
            (chat_id, gid))
        conn.commit()
        conn.close()
        return gid

    # No person or no group - create Standard group
    code = _generate_invite_code()
    cur = conn.execute(
        "INSERT INTO groups (name, invite_code, created_by) VALUES (?,?,?)",
        ("Standard", code, chat_id))
    gid = cur.lastrowid

    if person:
        # Update person's group_id
        conn.execute("UPDATE persons SET group_id=? WHERE id=?", (gid, person["id"]))
        conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, chat_id, person_id, role) "
            "VALUES (?,?,?,?)",
            (gid, chat_id, person["id"], "admin"))
    else:
        # Auto-create person with default_name (Telegram first_name) and link
        person_name = (default_name or "Ich").strip()
        cur2 = conn.execute(
            "INSERT INTO persons (name, chat_id, group_id) VALUES (?,?,?)",
            (person_name, chat_id, gid))
        new_pid = cur2.lastrowid
        conn.execute(
            "INSERT OR IGNORE INTO group_members (group_id, chat_id, person_id, role) "
            "VALUES (?,?,?,?)",
            (gid, chat_id, new_pid, "admin"))

    conn.execute(
        "INSERT OR REPLACE INTO active_groups (chat_id, group_id) VALUES (?,?)",
        (chat_id, gid))
    conn.commit()
    conn.close()
    return gid
