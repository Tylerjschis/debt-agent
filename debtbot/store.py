"""Local SQLite store. Bank tokens are encrypted at rest with Fernet."""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import date, datetime
from pathlib import Path

from cryptography.fernet import Fernet

from .engine import Debt, Plan

SCHEMA = """
CREATE TABLE IF NOT EXISTS institutions (
    item_id TEXT PRIMARY KEY, name TEXT, token_enc TEXT NOT NULL,
    cursor TEXT, status TEXT DEFAULT 'ok', updated_at TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY, item_id TEXT, name TEXT, official_name TEXT, mask TEXT,
    type TEXT, subtype TEXT, balance REAL DEFAULT 0, apr REAL DEFAULT 0,
    min_payment REAL DEFAULT 0, due_date TEXT, promo_end TEXT,
    deferred_interest INTEGER DEFAULT 0, pay_url TEXT, source TEXT,
    updated_at TEXT, excluded INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY, account_id TEXT, date TEXT, name TEXT,
    amount_in REAL, created_at TEXT, processed INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT, deposit REAL,
    tx_id TEXT, status TEXT DEFAULT 'proposed', summary TEXT
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, plan_id INTEGER, debt_id TEXT,
    name TEXT, minimum REAL, extra REAL, due_date TEXT, pay_url TEXT,
    reason TEXT, status TEXT DEFAULT 'proposed', paid_at TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, event TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT);
"""


def _d(s: str | None) -> date | None:
    return date.fromisoformat(s) if s else None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path, key: str | None):
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self._lock = threading.RLock()
        self.fernet = Fernet(key.encode()) if key else None

    # -- helpers -----------------------------------------------------------
    def _q(self, sql: str, args=()):
        with self._lock:
            return self.db.execute(sql, args).fetchall()

    def _x(self, sql: str, args=()):
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    def audit(self, event: str, detail: dict | str = ""):
        detail = detail if isinstance(detail, str) else json.dumps(detail, default=str)
        self._x("INSERT INTO audit(at, event, detail) VALUES (?,?,?)", (_now(), event, detail))

    # -- key/value (runtime settings changed from chat) --------------------
    def kv_get(self, key: str, default=None):
        r = self._q("SELECT value FROM kv WHERE key=?", (key,))
        return json.loads(r[0]["value"]) if r else default

    def kv_set(self, key: str, value):
        self._x("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))

    # -- institutions --------------------------------------------------------
    def add_institution(self, item_id: str, name: str, access_token: str):
        if not self.fernet:
            raise RuntimeError("DEBTBOT_KEY is not set; refusing to store a bank token unencrypted.")
        enc = self.fernet.encrypt(access_token.encode()).decode()
        self._x("INSERT OR REPLACE INTO institutions(item_id,name,token_enc,status,updated_at) VALUES(?,?,?,?,?)",
                (item_id, name, enc, "ok", _now()))
        self.audit("institution_linked", {"item_id": item_id, "name": name})

    def institutions(self) -> list[dict]:
        out = []
        for r in self._q("SELECT * FROM institutions"):
            d = dict(r)
            d["access_token"] = self.fernet.decrypt(d.pop("token_enc").encode()).decode()
            out.append(d)
        return out

    def set_institution(self, item_id: str, **fields):
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE institutions SET {cols} WHERE item_id=?", (*fields.values(), item_id))

    # -- accounts --------------------------------------------------------------
    def upsert_account(self, a: dict):
        a = {**a, "updated_at": a.get("updated_at") or _now()}
        existing = self._q("SELECT * FROM accounts WHERE id=?", (a["id"],))
        if existing:
            keep = {k: v for k, v in a.items() if v is not None}
            cols = ", ".join(f"{k}=?" for k in keep if k != "id")
            self._x(f"UPDATE accounts SET {cols} WHERE id=?",
                    (*[v for k, v in keep.items() if k != "id"], a["id"]))
        else:
            cols = ", ".join(a)
            self._x(f"INSERT INTO accounts({cols}) VALUES ({','.join('?' * len(a))})", tuple(a.values()))

    def update_account(self, account_id: str, **fields):
        cols = ", ".join(f"{k}=?" for k in fields)
        self._x(f"UPDATE accounts SET {cols} WHERE id=?", (*fields.values(), account_id))

    def accounts(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM accounts ORDER BY type, name")]

    def debts(self) -> list[Debt]:
        rows = self._q("SELECT * FROM accounts WHERE type IN ('credit','loan') AND excluded=0 AND balance > 0.005")
        return [
            Debt(
                id=r["id"], name=r["name"], balance=round(r["balance"] or 0, 2),
                apr=r["apr"] or 0, min_payment=r["min_payment"] or 0,
                due_date=_d(r["due_date"]), promo_end=_d(r["promo_end"]),
                deferred_interest=bool(r["deferred_interest"]), pay_url=r["pay_url"],
                source=r["source"] or "plaid", updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def checking(self, match: str | None = None) -> dict | None:
        rows = self._q("SELECT * FROM accounts WHERE type='depository' AND excluded=0 ORDER BY name")
        if match:
            rows = [r for r in rows if match.lower() in (r["name"] or "").lower()] or rows
        checking = [r for r in rows if (r["subtype"] or "") == "checking"] or rows
        return dict(checking[0]) if checking else None

    # -- transactions --------------------------------------------------------------
    def add_transaction(self, tx_id: str, account_id: str, on: date | str, name: str, amount_in: float):
        self._x("INSERT OR IGNORE INTO transactions(id,account_id,date,name,amount_in,created_at) VALUES(?,?,?,?,?,?)",
                (tx_id, account_id, str(on), name, amount_in, _now()))

    def remove_transaction(self, tx_id: str):
        self._x("DELETE FROM transactions WHERE id=? AND processed=0", (tx_id,))

    def unprocessed_deposits(self, account_id: str | None) -> list[dict]:
        sql = "SELECT * FROM transactions WHERE processed=0 AND amount_in > 0"
        args: tuple = ()
        if account_id:
            sql += " AND account_id=?"
            args = (account_id,)
        return [dict(r) for r in self._q(sql + " ORDER BY date", args)]

    def mark_processed(self, tx_id: str):
        self._x("UPDATE transactions SET processed=1 WHERE id=?", (tx_id,))

    def mark_all_processed(self):
        self._x("UPDATE transactions SET processed=1 WHERE processed=0")

    # -- plans & payments -------------------------------------------------------
    def save_plan(self, plan: Plan, tx_id: str | None) -> int:
        summary = {
            "deposit": plan.deposit, "checking": plan.checking, "next_payday": str(plan.next_payday),
            "bills": [(n, a, str(d)) for n, a, d in plan.bills_due], "buffer": plan.buffer,
            "shortfall": plan.shortfall, "carried": plan.carried, "notes": plan.notes,
        }
        pid = self._x("INSERT INTO plans(created_at,deposit,tx_id,summary) VALUES(?,?,?,?)",
                      (_now(), plan.deposit, tx_id, json.dumps(summary))).lastrowid
        for l in plan.lines:
            self._x("INSERT INTO payments(plan_id,debt_id,name,minimum,extra,due_date,pay_url,reason) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (pid, l.debt_id, l.name, l.minimum, l.extra, str(l.due_date) if l.due_date else None,
                     l.pay_url, l.reason))
        self.audit("plan_proposed", {"plan_id": pid, "total": plan.payments_total})
        return pid

    def plan(self, plan_id: int) -> dict | None:
        r = self._q("SELECT * FROM plans WHERE id=?", (plan_id,))
        return dict(r[0]) if r else None

    def latest_plan(self) -> dict | None:
        r = self._q("SELECT * FROM plans ORDER BY id DESC LIMIT 1")
        return dict(r[0]) if r else None

    def set_plan_status(self, plan_id: int, status: str):
        self._x("UPDATE plans SET status=? WHERE id=?", (status, plan_id))
        if status in ("approved", "skipped", "replaced"):
            new = {"approved": "approved", "skipped": "skipped", "replaced": "replaced"}[status]
            self._x("UPDATE payments SET status=? WHERE plan_id=? AND status='proposed'", (new, plan_id))
        self.audit(f"plan_{status}", {"plan_id": plan_id})

    def replace_open_plans(self, except_id: int):
        for r in self._q("SELECT id FROM plans WHERE status='proposed' AND id != ?", (except_id,)):
            self.set_plan_status(r["id"], "replaced")

    def payments(self, plan_id: int) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM payments WHERE plan_id=? ORDER BY id", (plan_id,))]

    def payment(self, payment_id: int) -> dict | None:
        r = self._q("SELECT * FROM payments WHERE id=?", (payment_id,))
        return dict(r[0]) if r else None

    def open_payments(self) -> list[dict]:
        return [dict(r) for r in self._q("SELECT * FROM payments WHERE status='approved' ORDER BY due_date")]

    def mark_payment_paid(self, payment_id: int):
        self._x("UPDATE payments SET status='paid', paid_at=? WHERE id=?", (_now(), payment_id))
        self.audit("payment_marked_paid", {"payment_id": payment_id})

    def covered_debt_ids(self, debts: list[Debt]) -> set[str]:
        """Debts whose current minimum is already in an approved or paid payment."""
        covered = set()
        for d in debts:
            if not d.due_date:
                continue
            r = self._q(
                "SELECT 1 FROM payments WHERE debt_id=? AND due_date=? AND minimum>0 AND status IN ('approved','paid')",
                (d.id, str(d.due_date)),
            )
            if r:
                covered.add(d.id)
        return covered
