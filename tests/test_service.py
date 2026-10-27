from datetime import date

from cryptography.fernet import Fernet

from debtbot.config import Config, is_paycheck
from debtbot.service import Service
from debtbot.store import Store


def make(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBTBOT_DB", str(tmp_path / "t.db"))
    monkeypatch.setenv("DEBTBOT_KEY", Fernet.generate_key().decode())
    cfg = Config()
    svc = Service(cfg, Store(cfg.db_path, cfg.db_key), today_fn=lambda: date(2026, 10, 23))
    svc.refresh()
    return svc


def test_paycheck_rules():
    rules = [{"match": "PAYROLL", "min_amount": 1000}]
    assert is_paycheck("ACME PAYROLL DIRECT DEP", 2500, rules)
    assert not is_paycheck("ACME PAYROLL", 50, rules)
    assert not is_paycheck("Venmo", 5000, rules)


def test_paycheck_to_paid_flow(tmp_path, monkeypatch):
    svc = make(tmp_path, monkeypatch)
    svc.fake_paycheck(4200)
    plan, pid = svc.build_plan(deposit=4200)
    assert plan.payments_total > 0
    assert plan.left_in_checking >= plan.bills_total + plan.buffer

    svc.store.set_plan_status(pid, "approved")
    # approved minimums aren't asked for again
    plan2, _ = svc.build_plan()
    assert all(l.minimum == 0 for l in plan2.lines)

    for p in svc.store.payments(pid):
        svc.mark_paid(p["id"])
    assert all(p["status"] == "paid" for p in svc.store.payments(pid))
    assert svc.snapshot()["total_debt"] < 25770


def test_settings_change_persists(tmp_path, monkeypatch):
    svc = make(tmp_path, monkeypatch)
    svc.update_settings(buffer=1500, strategy="snowball")
    s = svc.cfg.settings(svc.store)
    assert s.buffer == 1500 and s.strategy == "snowball"


def test_removed_manual_account_disappears(tmp_path, monkeypatch):
    svc = make(tmp_path, monkeypatch)
    svc.cfg.manual_accounts = [{"name": "Car Loan", "type": "loan", "balance": 1000, "apr": 5,
                                "min_payment": 100, "due_day": 5}]
    svc.refresh()
    assert any(d.name == "Car Loan" for d in svc.store.debts())
    svc.cfg.manual_accounts = []
    svc.refresh()
    assert not any(d.name == "Car Loan" for d in svc.store.debts())
