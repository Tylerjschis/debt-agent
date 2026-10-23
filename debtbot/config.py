"""Loads config.yaml and .env, and applies your per-account settings."""
from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path

import yaml
from dotenv import load_dotenv

from .engine import Bill, Settings, next_on_day
from .store import Store

ROOT = Path(__file__).resolve().parent.parent


class Config:
    def __init__(self, path: str | Path | None = None):
        load_dotenv(ROOT / ".env")
        path = Path(path or os.getenv("DEBTBOT_CONFIG") or ROOT / "config.yaml")
        with open(path) as f:
            self.raw: dict = yaml.safe_load(f) or {}
        self.mode: str = (self.raw.get("mode") or "demo").lower()
        self.income: dict = self.raw.get("income") or {}
        self.bills = [Bill(b["name"], float(b["amount"]), int(b["due_day"])) for b in self.raw.get("bills") or []]
        self.account_rules: list[dict] = self.raw.get("accounts") or []
        self.manual_accounts: list[dict] = self.raw.get("manual_accounts") or []
        s = self.raw.get("settings") or {}
        self.timezone: str = s.get("timezone", "America/New_York")
        self.check_minutes: int = int(s.get("check_interval_minutes", 15))
        self.reminder_days: int = int(s.get("reminder_days", 3))
        self.checking_match: str | None = s.get("checking_account")
        self._settings = s

        # secrets from .env
        self.telegram_token = os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.owner_id = int(os.getenv("TELEGRAM_USER_ID", "0") or 0)
        self.plaid_client_id = os.getenv("PLAID_CLIENT_ID", "")
        self.plaid_secret = os.getenv("PLAID_SECRET", "")
        self.plaid_env = os.getenv("PLAID_ENV", "sandbox")
        self.anthropic_key = os.getenv("ANTHROPIC_API_KEY", "")
        self.model = os.getenv("CLAUDE_MODEL", "claude-sonnet-5-5")
        self.db_key = os.getenv("DEBTBOT_KEY", "")
        self.db_path = os.getenv("DEBTBOT_DB", str(ROOT / "debtbot.db"))
        # Web page for connecting banks (/link). Railway sets PORT and RAILWAY_PUBLIC_DOMAIN.
        self.port = int(os.getenv("PORT", "8080"))
        domain = os.getenv("PUBLIC_URL") or os.getenv("RAILWAY_PUBLIC_DOMAIN") or ""
        if domain and not domain.startswith("http"):
            domain = "https://" + domain
        self.public_url = domain.rstrip("/") or f"http://localhost:{self.port}"
        self.plaid_redirect_uri = os.getenv("PLAID_REDIRECT_URI") or None

    def settings(self, store: Store | None = None) -> Settings:
        """Settings from config.yaml, with any changes you approved in chat on top."""
        s = self._settings
        merged = {
            "buffer": float(s.get("buffer", 1000)),
            "strategy": str(s.get("strategy", "avalanche")).lower(),
            "carry_threshold": float(s.get("carry_threshold", 50)),
            "promo_window_days": int(s.get("promo_window_days", 90)),
        }
        if store:
            merged.update(store.kv_get("settings", {}) or {})
        return Settings(**merged)

    def income_rules(self) -> list[dict]:
        return self.income.get("rules") or []


def is_paycheck(name: str, amount_in: float, rules: list[dict]) -> bool:
    """A deposit counts as pay if it matches any rule's text and minimum amount."""
    for r in rules:
        text = str(r.get("match", "")).lower()
        if text and text in (name or "").lower() and amount_in >= float(r.get("min_amount", 0)):
            return True
    return False


def apply_account_rules(store: Store, cfg: Config):
    """Nicknames, pay links, promo dates and exclusions from config.yaml."""
    for acct in store.accounts():
        haystack = " ".join(str(acct.get(k) or "") for k in ("name", "official_name", "mask")).lower()
        for rule in cfg.account_rules:
            m = str(rule.get("match", "")).lower()
            if not m or m not in haystack:
                continue
            fields = {}
            if rule.get("nickname"):
                fields["name"] = rule["nickname"]
            if rule.get("pay_url"):
                fields["pay_url"] = rule["pay_url"]
            if "promo_end" in rule:
                fields["promo_end"] = str(rule["promo_end"]) if rule["promo_end"] else None
            if "deferred_interest" in rule:
                fields["deferred_interest"] = int(bool(rule["deferred_interest"]))
            if rule.get("apr") is not None:
                fields["apr"] = float(rule["apr"])
            if "exclude" in rule:
                fields["excluded"] = int(bool(rule["exclude"]))
            if fields:
                store.update_account(acct["id"], **fields)
            break


def sync_manual_accounts(store: Store, cfg: Config, today: date):
    """Accounts Plaid can't see. Balance is entered once, then estimated monthly
    (balance + interest - payment) until you update it."""
    for m in cfg.manual_accounts:
        acct_id = "manual:" + m["name"].lower().replace(" ", "-")
        existing = {a["id"]: a for a in store.accounts()}.get(acct_id)
        due = next_on_day(int(m["due_day"]), today) if m.get("due_day") else None
        if existing is None:
            store.upsert_account({
                "id": acct_id, "name": m["name"], "type": m.get("type", "loan"), "subtype": "manual",
                "balance": float(m["balance"]), "apr": float(m.get("apr", 0)),
                "min_payment": float(m.get("min_payment", 0)), "due_date": str(due) if due else None,
                "promo_end": str(m["promo_end"]) if m.get("promo_end") else None,
                "deferred_interest": int(bool(m.get("deferred_interest"))), "pay_url": m.get("pay_url"),
                "source": "manual", "updated_at": str(today),
            })
            store.kv_set(f"manual_seed:{acct_id}", float(m["balance"]))
            continue
        # You changed the balance in config.yaml -> take it as the new truth.
        if float(m["balance"]) != store.kv_get(f"manual_seed:{acct_id}"):
            store.kv_set(f"manual_seed:{acct_id}", float(m["balance"]))
            store.update_account(acct_id, balance=float(m["balance"]), updated_at=str(today))
        # Roll the estimate forward past each due date that has passed.
        prev_due = date.fromisoformat(existing["due_date"]) if existing.get("due_date") else None
        bal = existing["balance"]
        while prev_due and due and prev_due < today:
            bal = bal * (1 + float(m.get("apr", 0)) / 1200) - float(m.get("min_payment", 0))
            prev_due = next_on_day(int(m["due_day"]), prev_due + timedelta(days=1))
        store.update_account(acct_id, balance=round(max(bal, 0), 2),
                             due_date=str(due) if due else None, pay_url=m.get("pay_url"),
                             apr=float(m.get("apr", 0)), min_payment=float(m.get("min_payment", 0)))


def seed_demo(store: Store, today: date):
    """Fake accounts so you can try the whole flow before connecting anything."""
    if store.accounts():
        return
    d = lambda n: str(today + timedelta(days=n))  # noqa: E731
    rows = [
        {"id": "demo-checking", "name": "Demo Checking", "type": "depository", "subtype": "checking",
         "balance": 2300.00},
        {"id": "demo-chase", "name": "Chase Sapphire", "type": "credit", "subtype": "credit card",
         "balance": 4850.00, "apr": 24.99, "min_payment": 95, "due_date": d(5),
         "pay_url": "https://www.chase.com"},
        {"id": "demo-cap1", "name": "Capital One Quicksilver", "type": "credit", "subtype": "credit card",
         "balance": 2140.00, "apr": 29.99, "min_payment": 60, "due_date": d(12),
         "pay_url": "https://www.capitalone.com"},
        {"id": "demo-disc", "name": "Discover It", "type": "credit", "subtype": "credit card",
         "balance": 3600.00, "apr": 22.99, "min_payment": 72, "due_date": d(18), "promo_end": d(60),
         "pay_url": "https://www.discover.com"},
        {"id": "demo-amex", "name": "Amex Blue Cash", "type": "credit", "subtype": "credit card",
         "balance": 980.00, "apr": 19.99, "min_payment": 40, "due_date": d(25),
         "pay_url": "https://www.americanexpress.com"},
    ]
    for r in rows:
        store.upsert_account({**r, "source": "demo"})
    store.audit("demo_seeded")
