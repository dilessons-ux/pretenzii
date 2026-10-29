"""База данных: SQLite, схема, журнал аудита без права изменения/удаления."""
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
DB_PATH = os.getenv("DB_PATH", os.path.join(os.path.dirname(__file__), "..", "data", "pretenzii.db"))

SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    phone TEXT,
    role TEXT NOT NULL,              -- director, np, rop, brigade, technolog, designer, supply, staff
    password_hash TEXT NOT NULL,
    telegram_chat_id TEXT,
    tg_link_code TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claims (
    id INTEGER PRIMARY KEY,
    number TEXT UNIQUE NOT NULL,
    status TEXT NOT NULL,            -- см. STATUSES в logic.py
    received_at TEXT NOT NULL,       -- когда клиент предъявил претензию (дата претензии)
    accepted_by INTEGER NOT NULL REFERENCES users(id),
    accepted_at TEXT NOT NULL,       -- когда зарегистрировали в системе
    client_name TEXT NOT NULL,
    client_phone TEXT NOT NULL,
    client_email TEXT,
    address TEXT,
    contract_no TEXT NOT NULL,
    contract_date TEXT NOT NULL,
    contract_type TEXT NOT NULL,     -- podryad | kupla
    contract_price REAL,             -- база для неустойки
    demand_type TEXT NOT NULL,       -- repair | remake | price_cut | refund | expenses
    description TEXT,
    owner_id INTEGER REFERENCES users(id),
    review_due TEXT NOT NULL,        -- срок рассмотрения (2 дня)
    plan_days INTEGER,               -- дней на исполнение
    plan_due TEXT,                   -- плановая дата исполнения
    legal_due TEXT NOT NULL,         -- текущий юридический срок
    original_legal_due TEXT NOT NULL,-- первоначальный срок, не меняется никогда
    decision TEXT,
    culprit TEXT,                    -- production, measure, design, technolog, montage, supplier, logistics, client, none
    culprit_user_id INTEGER REFERENCES users(id),
    rc_symptom TEXT, rc_cause TEXT, rc_escape TEXT, rc_action TEXT,
    client_token TEXT UNIQUE NOT NULL,
    client_confirmed_at TEXT,
    done_at TEXT,
    closed_at TEXT,
    csat INTEGER,
    csat_solved INTEGER,
    csat_comment TEXT,
    csat_sent_at TEXT,
    csat_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS claim_participants (
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    role TEXT NOT NULL,
    PRIMARY KEY (claim_id, user_id)
);

CREATE TABLE IF NOT EXISTS claim_files (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    kind TEXT NOT NULL,              -- scan, defect, after, act, other
    filename TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    uploaded_by INTEGER REFERENCES users(id),
    uploaded_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checkins (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    user_id INTEGER NOT NULL REFERENCES users(id),
    day TEXT NOT NULL,
    state TEXT NOT NULL,             -- done, problem, help
    comment TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS extensions (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    reason TEXT NOT NULL,
    supply_date TEXT,
    proposed_due TEXT NOT NULL,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL,
    sent_at TEXT,
    answer TEXT,                     -- agree | disagree | NULL
    answered_at TEXT
);

CREATE TABLE IF NOT EXISTS costs (
    id INTEGER PRIMARY KEY,
    claim_id INTEGER NOT NULL REFERENCES claims(id),
    kind TEXT NOT NULL,              -- work, materials, fittings, visit, compensation, penalty
    amount REAL NOT NULL,
    note TEXT,
    charge_to TEXT,                  -- виновник: brigade, technolog, designer, supplier, company
    charge_user_id INTEGER REFERENCES users(id),
    approval TEXT NOT NULL,          -- approved | pending_director | rejected
    approved_by INTEGER REFERENCES users(id),
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    claim_id INTEGER,
    user_id INTEGER,
    actor TEXT,                      -- имя или 'система' / 'клиент'
    action TEXT NOT NULL,
    field TEXT,
    old_value TEXT,
    new_value TEXT,
    comment TEXT
);

CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

-- Запрет удаления претензий и подмены первоначального срока
CREATE TRIGGER IF NOT EXISTS claims_no_delete BEFORE DELETE ON claims
BEGIN SELECT RAISE(ABORT, 'claims cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS claims_keep_original_due BEFORE UPDATE OF original_legal_due ON claims
WHEN NEW.original_legal_due <> OLD.original_legal_due
BEGIN SELECT RAISE(ABORT, 'original_legal_due is immutable'); END;

CREATE TABLE IF NOT EXISTS job_runs (
    job TEXT NOT NULL,
    run_key TEXT NOT NULL,
    PRIMARY KEY (job, run_key)
);

CREATE TABLE IF NOT EXISTS tg_pending (
    chat_id TEXT PRIMARY KEY,
    claim_id INTEGER NOT NULL,
    state TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_claim ON audit_log(claim_id);
CREATE INDEX IF NOT EXISTS idx_checkins_claim_day ON checkins(claim_id, day);
"""


def now() -> datetime:
    return datetime.now(TZ)


def now_iso() -> str:
    return now().strftime("%Y-%m-%d %H:%M:%S")


def connect() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    with tx() as c:
        c.executescript(SCHEMA)


def audit(c, claim_id, user, action, field=None, old=None, new=None, comment=None):
    """user: sqlite Row пользователя, строка ('система', 'клиент') или None."""
    if user is None or isinstance(user, str):
        uid, actor = None, (user or "система")
    else:
        uid, actor = user["id"], user["name"]
    c.execute(
        "INSERT INTO audit_log (ts, claim_id, user_id, actor, action, field, old_value, new_value, comment)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (now_iso(), claim_id, uid, actor, action, field,
         None if old is None else str(old), None if new is None else str(new), comment),
    )


def set_field(c, claim, user, field, value, comment=None):
    """Меняет поле претензии и пишет старое/новое значение в журнал."""
    old = claim[field]
    if (old if old is not None else "") == (value if value is not None else ""):
        return False
    c.execute(f"UPDATE claims SET {field} = ? WHERE id = ?", (value, claim["id"]))
    audit(c, claim["id"], user, "изменение", field, old, value, comment)
    return True


def job_once(c, job: str, key: str) -> bool:
    """True, если задача с таким ключом ещё не выполнялась (и отмечает её)."""
    try:
        c.execute("INSERT INTO job_runs (job, run_key) VALUES (?, ?)", (job, key))
        return True
    except sqlite3.IntegrityError:
        return False
