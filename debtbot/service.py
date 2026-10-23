"""Glue between the store, the engine and the bank sync.

The Telegram bot and the AI chat both call these functions, so the same
numbers come out no matter how you ask.
"""
from __future__ import annotations

import logging
import threading
import uuid
from datetime import date, timedelta

from . import plaid_sync
from .config import Config, apply_account_rules, is_paycheck, seed_demo, sync_manual_accounts
from .engine import Plan, next_payday, plan_paycheck, simulate_payoff
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

    def next_pay(self) -> date:
        return next_payday(self.cfg.income, self.today())

    def build_plan(self, deposit: float = 0.0, tx_id: str | None = None,
                   overrides: dict[str, float] | None = None) -> tuple[Plan, int]:
        chk = self.checking_account()
        debts = self.store.debts()
        plan = plan_paycheck(
            checking=chk["balance"] if chk else 0.0,
            deposit=deposit,
            debts=debts,
            bills=self.cfg.bills,
            settings=self.cfg.settings(self.store),
            today=self.today(),
            next_pay=self.next_pay(),
            already_covered=self.store.covered_debt_ids(debts),
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
        for tx in self.store.unprocessed_deposits(chk["id"] if chk else None):
            if is_paycheck(tx["name"], tx["amount_in"], self.cfg.income_rules()):
                found.append(tx)
            self.store.mark_processed(tx["id"])
        return found

    def fake_paycheck(self, amount: float) -> dict:
        """Testing: pretend a paycheck just landed."""
        chk = self.checking_account()
        if chk is None:
            raise RuntimeError("No checking account yet.")
        tx_id = f"fake-{uuid.uuid4().hex[:10]}"
        self.store.add_transaction(tx_id, chk["id"], self.today(), "TEST PAYCHECK", amount)
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
            "checking_balance": chk["balance"] if chk else None,
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
            "bills": [{"name": n, "amount": a, "due": str(d)} for n, a, d in bills_due_between(self.cfg.bills, today, end)],
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
