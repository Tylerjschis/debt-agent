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
    wanted = {"manual:" + m["name"].lower().replace(" ", "-") for m in cfg.manual_accounts}
    for a in store.accounts():  # removed from config.yaml -> removed here too
        if a["id"].startswith("manual:") and a["id"] not in wanted:
            store.delete_account(a["id"])
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


def seed_demo_activity(store: Store, today: date):
    """Four months of made-up transactions and recurring charges for demo mode,
    so bills, subscriptions and spending can be tried before Plaid is connected."""
    import random

    if store.streams(active_only=False):
        return
    rnd = random.Random(7)
    PRIMARY = ("FOOD_AND_DRINK", "TRANSPORTATION", "GENERAL_MERCHANDISE", "HOME_IMPROVEMENT", "BANK_FEES",
               "ENTERTAINMENT", "PERSONAL_CARE", "GENERAL_SERVICES", "RENT_AND_UTILITIES", "LOAN_PAYMENTS",
               "INCOME", "TRANSFER_OUT")

    def primary(det: str) -> str:
        return next(p for p in PRIMARY if det.startswith(p))

    def last_on_day(day: int) -> date:
        d = next_on_day(day, today - timedelta(days=31))
        while next_on_day(day, d + timedelta(days=1)) <= today:
            d = next_on_day(day, d + timedelta(days=1))
        return d

    def nxt(d: date) -> date:
        return next_on_day(d.day, d + timedelta(days=1))

    # A Friday paycheck every two weeks
    last_pay = today - timedelta(days=(today.weekday() - 4) % 7 or 7)
    streams = [
        # direction, account, name, detailed, amount (avg, last), frequency, last date
        ("in", "demo-checking", "ACME CORP PAYROLL", "INCOME_WAGES", (4200, 4200), "biweekly", last_pay),
        ("out", "demo-checking", "Mortgage", "LOAN_PAYMENTS_MORTGAGE_PAYMENT", (1850, 1850), "monthly", last_on_day(1)),
        ("out", "demo-checking", "BGE Electric", "RENT_AND_UTILITIES_GAS_AND_ELECTRICITY", (176.4, 189.2), "monthly", last_on_day(18)),
        ("out", "demo-checking", "Verizon", "RENT_AND_UTILITIES_TELEPHONE", (210, 210), "monthly", last_on_day(24)),
        ("out", "demo-checking", "Progressive Insurance", "GENERAL_SERVICES_INSURANCE", (185, 185), "monthly", last_on_day(9)),
        ("out", "demo-checking", "Chase Card Autopay", "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT", (95, 95), "monthly", last_on_day(28)),
        ("out", "demo-chase", "Netflix", "ENTERTAINMENT_TV_AND_MOVIES", (22.99, 22.99), "monthly", last_on_day(3)),
        ("out", "demo-amex", "Hulu", "ENTERTAINMENT_TV_AND_MOVIES", (17.99, 17.99), "monthly", last_on_day(11)),
        ("out", "demo-cap1", "Max", "ENTERTAINMENT_TV_AND_MOVIES", (16.99, 16.99), "monthly", last_on_day(14)),
        ("out", "demo-chase", "Spotify", "ENTERTAINMENT_MUSIC_AND_AUDIO", (11.99, 11.99), "monthly", last_on_day(6)),
        ("out", "demo-amex", "Audible", "ENTERTAINMENT_MUSIC_AND_AUDIO", (14.95, 14.95), "monthly", last_on_day(20)),
        ("out", "demo-cap1", "Planet Fitness", "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS", (24.99, 24.99), "monthly", last_on_day(15)),
        ("out", "demo-chase", "Adobe", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", (56.66, 59.99), "monthly", last_on_day(8)),
        ("out", "demo-chase", "Apple iCloud", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES", (2.99, 2.99), "monthly", last_on_day(2)),
    ]
    tx_n = 0

    def tx(acct, d, name, det, amount_in):
        nonlocal tx_n
        tx_n += 1
        store.add_transaction(f"demo-tx-{tx_n}", acct, d, name, round(amount_in, 2),
                              merchant=name, category=primary(det), detailed=det)

    start = today - timedelta(days=120)
    for i, (direction, acct, name, det, (avg, last), freq, last_d) in enumerate(streams):
        nd = last_d + timedelta(days=14) if freq == "biweekly" else nxt(last_d)
        store.upsert_stream({
            "stream_id": f"demo-stream-{i}", "item_id": "demo", "account_id": acct, "direction": direction,
            "name": name, "merchant": name, "frequency": freq, "avg_amount": avg, "last_amount": last,
            "last_date": str(last_d), "next_date": str(nd), "category": primary(det), "detailed": det,
            "status": "MATURE", "active": 1,
        })
        d = last_d
        while d >= start:
            amt = last if d == last_d else avg
            tx(acct, d, name, det, amt if direction == "in" else -amt)
            d = d - timedelta(days=14) if freq == "biweekly" else next_on_day(d.day, d - timedelta(days=31))
    # One yearly charge
    prime = today - timedelta(days=200)
    store.upsert_stream({
        "stream_id": "demo-stream-prime", "item_id": "demo", "account_id": "demo-chase", "direction": "out",
        "name": "Amazon Prime", "merchant": "Amazon Prime", "frequency": "annually", "avg_amount": 139,
        "last_amount": 139, "last_date": str(prime), "next_date": str(prime + timedelta(days=365)),
        "category": "GENERAL_MERCHANDISE", "detailed": "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
        "status": "MATURE", "active": 1,
    })

    # Everyday spending; food delivery jumps in the last month
    d = start
    while d <= today:
        recent = (today - d).days < 30
        if rnd.random() < 0.2:
            tx("demo-chase", d, rnd.choice(["Giant", "Weis Markets"]), "FOOD_AND_DRINK_GROCERIES", -rnd.uniform(70, 180))
        if rnd.random() < 0.25:
            tx("demo-cap1", d, "Sheetz", "TRANSPORTATION_GAS", -rnd.uniform(40, 70))
        if rnd.random() < 0.55:
            tx("demo-amex", d, rnd.choice(["Starbucks", "Dunkin"]), "FOOD_AND_DRINK_COFFEE", -rnd.uniform(4.5, 9))
        if rnd.random() < (0.45 if recent else 0.14):
            tx("demo-chase", d, "DoorDash", "FOOD_AND_DRINK_RESTAURANT", -rnd.uniform(24, 46))
        if rnd.random() < 0.13:
            tx("demo-amex", d, rnd.choice(["Olive Garden", "Firestone's", "Chipotle"]),
               "FOOD_AND_DRINK_RESTAURANT", -rnd.uniform(18, 110))
        if rnd.random() < 0.22:
            tx("demo-chase", d, "Amazon", "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", -rnd.uniform(12, 85))
        if rnd.random() < 0.04:
            tx("demo-cap1", d, "Home Depot", "HOME_IMPROVEMENT_HARDWARE", -rnd.uniform(30, 240))
        if rnd.random() < 0.03:
            tx("demo-checking", d, "ATM Fee", "BANK_FEES_ATM_FEES", -3.5)
        d += timedelta(days=1)
    store.mark_all_processed()
    store.audit("demo_activity_seeded", {"transactions": tx_n})
