"""Glue between the store, the engine and the bank sync.

The Telegram bot and the AI chat both call these functions, so the same
numbers come out no matter how you ask.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import date, timedelta

from . import insights, plaid_sync
from .config import (Config, apply_account_rules, is_paycheck, seed_demo, seed_demo_activity,
                     sync_manual_accounts)
from .engine import Bill, Plan, _advance, next_payday, plan_paycheck, simulate_payoff
from .store import Store

log = logging.getLogger(__name__)


class Service:
    def __init__(self, cfg: Config, store: Store, today_fn=date.today):
        self.cfg = cfg
        self.store = store
        self.today = today_fn
        self._plaid = None
        self._refresh_lock = threading.Lock()
        if cfg.mode == "plaid":
            if not (cfg.plaid_client_id and cfg.plaid_secret):
                raise RuntimeError("mode is 'plaid' but PLAID_CLIENT_ID / PLAID_SECRET are missing from .env")
            self._plaid = plaid_sync.client(cfg.plaid_client_id, cfg.plaid_secret, cfg.plaid_env)

    @property
    def plaid(self):
        return self._plaid

    # -- data refresh ----------------------------------------------------------
    def refresh(self) -> list[str]:
        """Pull fresh bank data. Returns banks that need a re-login."""
        with self._refresh_lock:
            return self._refresh()

    def _refresh(self) -> list[str]:
        today = self.today()
        broken: list[str] = []
        if self.cfg.mode == "demo":
            seed_demo(self.store, today)
            seed_demo_activity(self.store, today)
        elif self._plaid:
            broken = plaid_sync.sync_all(self._plaid, self.store)
        sync_manual_accounts(self.store, self.cfg, today)
        apply_account_rules(self.store, self.cfg)
        return broken

    def link_sandbox(self) -> str:
        if not self._plaid or self.cfg.plaid_env != "sandbox":
            raise RuntimeError("Sandbox linking needs mode: plaid and PLAID_ENV=sandbox")
        return plaid_sync.link_sandbox_bank(self._plaid, self.store)

    # -- core numbers ------------------------------------------------------------
    def checking_account(self) -> dict | None:
        return self.store.checking(self.cfg.checking_match)

    def _auto_income(self) -> bool:
        return (self.cfg.income.get("schedule") or "auto").lower() == "auto"

    def next_pay(self) -> date:
        """Next expected paycheck: from your detected pay pattern, or config.yaml."""
        today = self.today()
        if self._auto_income():
            inc = insights.income_streams(self.store.streams("in"))
            if inc:
                d = insights.next_due(inc[0], today) or today
                while d <= today:
                    d = _advance(d, inc[0].get("frequency") or "biweekly")
                return d
            return today + timedelta(days=int(self.cfg.income.get("horizon_days") or 14))
        return next_payday(self.cfg.income, today)

    def bills(self) -> list[Bill]:
        """Bills paid from checking: detected from your transactions, plus any in config.yaml."""
        return list(self.cfg.bills) + insights.checking_bills(self.store.streams("out"), self.today())

    def build_plan(self, deposit: float = 0.0, tx_id: str | None = None,
                   overrides: dict[str, float] | None = None) -> tuple[Plan, int]:
        chk = self.checking_account()
        debts = self.store.debts()
        bills = self.bills()
        covered = self.store.covered_debt_ids(debts)
        # A loan already paid by a detected bill (mortgage, car) isn't counted twice.
        accts = {a["id"]: a for a in self.store.accounts()}
        for d in debts:
            if accts.get(d.id, {}).get("type") == "loan" and d.min_payment and any(
                    abs(b.amount - d.min_payment) <= 0.1 * d.min_payment for b in bills if b.next_date):
                covered.add(d.id)
        plan = plan_paycheck(
            checking=chk["balance"] if chk else 0.0,
            deposit=deposit,
            debts=debts,
            bills=bills,
            settings=self.cfg.settings(self.store),
            today=self.today(),
            next_pay=self.next_pay(),
            already_covered=covered,
            overrides=overrides,
        )
        if chk is None:
            plan.notes.append("No checking account found yet, so the plan assumes $0 available.")
        stale = [d.name for d in debts if d.source in ("manual", "statement") and d.updated_at
                 and date.fromisoformat(d.updated_at[:10]) < self.today() - timedelta(days=45)]
        if stale:
            plan.notes.append("Estimated, not live (update soon): " + ", ".join(stale))
        return plan, self.store.save_plan(plan, tx_id)

    def new_paychecks(self) -> list[dict]:
        """Unprocessed deposits into checking that match your pay rules."""
        chk = self.checking_account()
        found = []
        inc = insights.income_streams(self.store.streams("in")) if self._auto_income() else []
        for tx in self.store.unprocessed_deposits(chk["id"] if chk else None):
            if is_paycheck(tx["name"], tx["amount_in"], self.cfg.income_rules()) or self._matches_income(tx, inc):
                found.append(tx)
            self.store.mark_processed(tx["id"])
        return found

    @staticmethod
    def _matches_income(tx: dict, inc: list[dict]) -> bool:
        """A deposit looks like pay: same payer as a detected pay stream (or labeled
        as income), and at least half the usual amount."""
        if not inc:
            return tx.get("category") == "INCOME" and tx["amount_in"] >= 500
        text = f"{tx.get('merchant') or ''} {tx.get('name') or ''}".lower()
        for s in inc:
            key = (s.get("merchant") or s.get("name") or "").lower()[:14].strip()
            same_payer = bool(key) and key in text
            if (same_payer or tx.get("category") == "INCOME") and tx["amount_in"] >= 0.5 * (s.get("avg_amount") or 0):
                return True
        return False

    def fake_paycheck(self, amount: float) -> dict:
        """Testing: pretend a paycheck just landed."""
        chk = self.checking_account()
        if chk is None:
            raise RuntimeError("No checking account yet.")
        tx_id = f"fake-{uuid.uuid4().hex[:10]}"
        self.store.add_transaction(tx_id, chk["id"], self.today(), "TEST PAYCHECK", amount,
                                   category="INCOME", detailed="INCOME_WAGES")
        self.store.update_account(chk["id"], balance=round(chk["balance"] + amount, 2))
        self.store.mark_processed(tx_id)
        self.store.audit("fake_paycheck", {"amount": amount})
        return {"id": tx_id, "amount_in": amount}

    def mark_paid(self, payment_id: int) -> dict | None:
        p = self.store.payment(payment_id)
        if not p or p["status"] == "paid":
            return p
        self.store.mark_payment_paid(payment_id)
        # Until the bank reports it, reflect the payment locally.
        amount = (p["minimum"] or 0) + (p["extra"] or 0)
        accts = {a["id"]: a for a in self.store.accounts()}
        debt = accts.get(p["debt_id"])
        if debt and debt["source"] in ("demo", "manual"):
            self.store.update_account(debt["id"], balance=round(max(debt["balance"] - amount, 0), 2))
        chk = self.checking_account()
        if chk and chk["source"] == "demo":
            self.store.update_account(chk["id"], balance=round(chk["balance"] - amount, 2))
        return self.store.payment(payment_id)

    # -- summaries ---------------------------------------------------------------
    def snapshot(self) -> dict:
        today = self.today()
        debts = self.store.debts()
        chk = self.checking_account()
        s = self.cfg.settings(self.store)
        return {
            "today": str(today),
            "checking_account": chk["name"] if chk else None,
            "checking_balance": chk["balance"] if chk else None,
            "cash_accounts": [{"name": a["name"], "type": a["subtype"], "mask": a.get("mask"),
                               "balance": a["balance"], "available": a.get("available")}
                              for a in self.store.cash_accounts()],
            "linked_banks": [{"name": i["name"], "status": i["status"], "problem": i.get("last_error")}
                             for i in self.store.institution_list()],
            "buffer": s.buffer,
            "strategy": s.strategy,
            "next_payday": str(self.next_pay()),
            "total_debt": round(sum(d.balance for d in debts), 2),
            "debts": [
                {
                    "name": d.name, "balance": d.balance, "apr": d.apr,
                    "apr_now": d.effective_apr(today), "min_payment": d.min_payment,
                    "due_date": str(d.due_date) if d.due_date else None,
                    "promo_end": str(d.promo_end) if d.promo_end else None,
                    "deferred_interest": d.deferred_interest, "source": d.source,
                    "updated": (d.updated_at or "")[:10],
                }
                for d in sorted(debts, key=lambda x: -x.effective_apr(today))
            ],
        }

    def compare(self, monthly_extra: float) -> dict:
        debts = self.store.debts()
        s = self.cfg.settings(self.store)
        out = {}
        for strat in ("avalanche", "snowball"):
            r = simulate_payoff(debts, monthly_extra=monthly_extra, strategy=strat, today=self.today(),
                                promo_window_days=s.promo_window_days)
            out[strat] = {
                "debt_free": str(r.debt_free) if r.debt_free else "not within 50 years",
                "months": r.months, "total_interest": r.total_interest,
                "payoff_order": sorted(r.payoff_month, key=r.payoff_month.get),
            }
        return out

    def simulate(self, monthly_extra: float = 0.0, lump_sum: float = 0.0,
                 lump_target: str | None = None, strategy: str | None = None) -> dict:
        s = self.cfg.settings(self.store)
        r = simulate_payoff(self.store.debts(), monthly_extra=monthly_extra, strategy=strategy or s.strategy,
                            today=self.today(), lump_sum=lump_sum, lump_target=lump_target,
                            promo_window_days=s.promo_window_days)
        return {
            "strategy": r.strategy, "debt_free": str(r.debt_free) if r.debt_free else "not within 50 years",
            "months": r.months, "total_interest": r.total_interest,
            "payoff_month_by_debt": r.payoff_month,
        }

    def upcoming(self, days: int = 14) -> dict:
        today = self.today()
        end = today + timedelta(days=days)
        from .engine import bills_due_between
        return {
            "bills": [{"name": n, "amount": a, "due": str(d)} for n, a, d in bills_due_between(self.bills(), today, end)],
            "debt_payments": [
                {"name": d.name, "minimum": d.min_payment, "due": str(d.due_date)}
                for d in self.store.debts() if d.due_date and today <= d.due_date <= end
            ],
        }

    def update_settings(self, **changes) -> dict:
        allowed = {"buffer", "strategy", "carry_threshold", "promo_window_days"}
        current = self.store.kv_get("settings", {}) or {}
        for k, v in changes.items():
            if k in allowed and v is not None:
                current[k] = v
        self.store.kv_set("settings", current)
        self.store.audit("settings_changed", changes)
        return current

    def reminders(self) -> list[dict]:
        """Approved-but-unpaid payments due soon, and minimums due soon that aren't in any plan."""
        today = self.today()
        soon = today + timedelta(days=self.cfg.reminder_days)
        out = []
        for p in self.store.open_payments():
            if p["due_date"] and date.fromisoformat(p["due_date"]) <= soon:
                out.append({"kind": "unpaid", **p})
        debts = self.store.debts()
        covered = self.store.covered_debt_ids(debts)
        for d in debts:
            if d.due_date and today <= d.due_date <= soon and d.id not in covered and d.min_payment > 0:
                out.append({"kind": "unplanned", "name": d.name, "minimum": d.min_payment,
                            "due_date": str(d.due_date), "pay_url": d.pay_url})
            if d.promo_end and (d.promo_end - today).days in (90, 60, 30, 14):
                out.append({"kind": "promo", "name": d.name, "promo_end": str(d.promo_end),
                            "balance": d.balance, "deferred_interest": d.deferred_interest})
        return out

    # -- recurring charges, subscriptions, spending ------------------------------------
    def recurring_overview(self) -> dict:
        today = self.today()
        streams = self.store.streams()
        bills = insights.checking_bills(streams, today)
        inc = insights.income_streams(streams)
        return {
            "income": [{"name": s.get("merchant") or s["name"], "amount": s["avg_amount"],
                        "frequency": s["frequency"], "next": str(insights.next_due(s, today) or "")} for s in inc],
            "next_payday": str(self.next_pay()),
            "bills_from_checking": [{"name": b.name, "amount": b.amount, "frequency": b.frequency,
                                     "next": str(b.next_date)} for b in sorted(bills, key=lambda b: b.next_date)]
                                   + [{"name": b.name, "amount": b.amount, "frequency": "monthly",
                                       "next": f"day {b.due_day} (from config.yaml)"} for b in self.cfg.bills],
            "monthly_bills_total": round(sum(insights.monthly(b.amount, b.frequency) for b in bills)
                                         + sum(b.amount for b in self.cfg.bills), 2),
        }

    def subscriptions(self) -> dict:
        return insights.subscriptions(self.store.streams("out"), self.today())

    def spending(self, days: int = 30) -> dict:
        today = self.today()
        start = today - timedelta(days=days - 1)
        history = self.store.transactions_between(start - timedelta(days=3 * days), today)
        return insights.spending(history, start, today, history)

    def find_transactions(self, text: str = "", days: int = 30, limit: int = 40) -> list[dict]:
        today = self.today()
        rows = self.store.transactions_between(today - timedelta(days=days), today)
        text = text.lower().strip()
        hits = [r for r in rows if not text or text in f"{r.get('merchant') or ''} {r['name']} {r.get('category') or ''}".lower()]
        return [{"date": r["date"], "merchant": r.get("merchant") or r["name"], "amount": -r["amount_in"],
                 "category": insights.pretty_category(r.get("category")), "account": r.get("account_name")}
                for r in hits[-limit:]]

    def set_stream_kind(self, pk: int, kind: str) -> dict | None:
        self.store.set_stream(pk, kind=kind)
        if kind == "cancel":
            s = self.store.stream(pk)
            self.store.kv_set(f"cancel_seen:{pk}", s.get("last_date") if s else None)
        self.store.audit("stream_marked", {"stream": pk, "kind": kind})
        return self.store.stream(pk)

    def recap(self) -> dict:
        """Last 7 days, for the Sunday message."""
        today = self.today()
        subs = self.subscriptions()["subscriptions"]
        soon = [s for s in subs if s["next"] and today < date.fromisoformat(s["next"]) <= today + timedelta(days=7)]
        return {"spending": self.spending(7), "renewing_this_week": soon,
                "total_debt": round(sum(d.balance for d in self.store.debts()), 2)}

    def recurring_events(self) -> list[dict]:
        """New recurring charges and price increases, each reported once."""
        events = []
        for s in self.store.streams():
            k = insights.kind(s)
            if not s.get("notified"):
                self.store.set_stream(s["id"], notified=1)
                if k not in ("transfer", "other_in"):
                    events.append({**s, "type": "new", "kind": k})
            if s.get("kind") == "cancel":
                seen = self.store.kv_get(f"cancel_seen:{s['id']}")
                if s.get("last_date") and seen and s["last_date"] > seen:
                    self.store.kv_set(f"cancel_seen:{s['id']}", s["last_date"])
                    events.append({**s, "type": "still_charging", "kind": k})
            last, avg = s.get("last_amount") or 0, s.get("avg_amount") or 0
            # Utility bills swing month to month; price alerts are for subscriptions.
            if (s["direction"] == "out" and k == "subscription" and last > avg * 1.05 and last - avg >= 1
                    and s.get("price_alert_date") != s.get("last_date")):
                self.store.set_stream(s["id"], price_alert_date=s.get("last_date"))
                events.append({**s, "type": "price_up", "kind": k})
        return events

    # -- budget -----------------------------------------------------------------------
    def budget_limits(self) -> dict[str, float]:
        return {k: float(v) for k, v in (self.store.kv_get("budget", {}) or {}).items()}

    def budget_status(self, month: str | None = None) -> dict:
        from . import budget
        today = self.today()
        start, _ = budget.month_bounds(month, today)
        txs = self.store.transactions_between(start, start + timedelta(days=31))
        st = budget.status(txs, self.budget_limits(), today, month)
        if not st["has_budget"]:
            st["suggested"] = self.budget_suggestion()
        return st

    def budget_suggestion(self) -> dict:
        from . import budget
        today = self.today()
        txs = self.store.transactions_between(today.replace(day=1) - timedelta(days=100), today)
        s = budget.suggest(txs, today)
        return {"limits": s, "total": round(sum(s.values()), 2),
                "names": {k: insights.pretty_category(k) for k in s}}

    def set_budget(self, limits: dict[str, float | None], replace: bool = False) -> dict[str, float]:
        """Set limits by category key. A value of None or 0 removes that category."""
        current = {} if replace else self.budget_limits()
        for k, v in limits.items():
            key = str(k).upper().replace(" & ", "_AND_").replace(" ", "_")
            if v in (None, 0, "") or float(v) <= 0:
                current.pop(key, None)
            else:
                current[key] = round(float(v), 2)
        self.store.kv_set("budget", current)
        self.store.audit("budget_set", current)
        return current

    def payoff_projection(self, monthly_extra: float = 0.0) -> dict:
        """Minimums only vs your plan with extra, for the Debt chart."""
        debts = self.store.debts()
        s = self.cfg.settings(self.store)
        base = simulate_payoff(debts, monthly_extra=0, strategy=s.strategy, today=self.today(),
                               promo_window_days=s.promo_window_days)
        plan = simulate_payoff(debts, monthly_extra=monthly_extra, strategy=s.strategy, today=self.today(),
                               promo_window_days=s.promo_window_days)

        def pack(r):
            return {"debt_free": str(r.debt_free) if r.debt_free else None, "months": r.months,
                    "total_interest": r.total_interest, "balances": r.balances, "capped": r.capped,
                    "payoff_order": sorted(r.payoff_month, key=r.payoff_month.get), "payoff_month": r.payoff_month}
        return {"extra": monthly_extra, "strategy": s.strategy, "minimums_only": pack(base), "with_extra": pack(plan),
                "monthly_minimums": round(sum(d.min_payment for d in debts), 2)}
