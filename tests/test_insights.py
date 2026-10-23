import sqlite3
from datetime import date, timedelta

from cryptography.fernet import Fernet

from debtbot import insights
from debtbot.config import Config
from debtbot.engine import Bill, bills_due_between
from debtbot.service import Service
from debtbot.store import Store

TODAY = date(2026, 10, 23)


def stream(**kw):
    base = {"id": 1, "direction": "out", "name": "X", "merchant": None, "frequency": "monthly",
            "avg_amount": 10, "last_amount": 10, "last_date": "2026-10-03", "next_date": "2026-11-03",
            "category": None, "detailed": None, "kind": None, "account_type": "credit", "account_name": "Card"}
    return {**base, **kw}


def test_detected_bill_dates():
    bills = [Bill("Weekly", 20, next_date=date(2026, 10, 20), frequency="weekly"),
             Bill("Monthly", 100, next_date=date(2026, 9, 30), frequency="monthly")]
    got = bills_due_between(bills, TODAY, TODAY + timedelta(days=14))
    assert [(n, d) for n, _, d in got] == [("Weekly", date(2026, 10, 27)), ("Monthly", date(2026, 10, 30)),
                                          ("Weekly", date(2026, 11, 3))]


def test_auto_kinds():
    assert insights.auto_kind(stream(category="ENTERTAINMENT", detailed="ENTERTAINMENT_TV_AND_MOVIES")) == "subscription"
    assert insights.auto_kind(stream(category="RENT_AND_UTILITIES", avg_amount=60)) == "bill"
    assert insights.auto_kind(stream(category="LOAN_PAYMENTS", detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT")) == "transfer"
    assert insights.auto_kind(stream(category="GENERAL_SERVICES", detailed="GENERAL_SERVICES_INSURANCE", avg_amount=90)) == "bill"
    assert insights.auto_kind(stream(direction="in", category="INCOME")) == "income"
    assert insights.kind(stream(category="ENTERTAINMENT", kind="ignore")) == "ignore"


def test_checking_bills_skip_cards_and_transfers():
    s = [stream(id=1, account_type="depository", category="RENT_AND_UTILITIES", avg_amount=200),
         stream(id=2, account_type="credit", category="ENTERTAINMENT"),
         stream(id=3, account_type="depository", category="LOAN_PAYMENTS", detailed="LOAN_PAYMENTS_CREDIT_CARD_PAYMENT"),
         stream(id=4, account_type="depository", category="RENT_AND_UTILITIES", kind="ignore")]
    bills = insights.checking_bills(s, TODAY)
    assert len(bills) == 1 and bills[0].amount == 200 and bills[0].next_date == date(2026, 11, 3)


def test_subscriptions_costs_and_overlaps():
    s = [stream(id=1, name="Netflix", detailed="ENTERTAINMENT_TV_AND_MOVIES", avg_amount=20, last_amount=22.99),
         stream(id=2, name="Hulu", detailed="ENTERTAINMENT_TV_AND_MOVIES", avg_amount=18, last_amount=18),
         stream(id=3, name="Prime", frequency="annually", avg_amount=139, last_amount=139,
                detailed="GENERAL_MERCHANDISE_ONLINE_MARKETPLACES", kind="cancel")]
    r = insights.subscriptions(s, TODAY)
    assert r["monthly_total"] == round(22.99 + 18 + 139 / 12, 2)
    assert next(x for x in r["subscriptions"] if x["name"] == "Netflix")["price_up"] == 2.99
    assert list(r["overlaps"].values()) == [["Netflix", "Hulu"]]
    assert r["flagged_to_cancel"] == ["Prime"]


def test_spending_vs_normal_and_habits():
    txs = []
    for k in range(120):
        d = TODAY - timedelta(days=k)
        txs.append({"date": str(d), "amount_in": -10, "category": "FOOD_AND_DRINK", "merchant": "Cafe", "name": "Cafe"})
        if k < 30:
            txs.append({"date": str(d), "amount_in": -20, "category": "FOOD_AND_DRINK", "merchant": "DoorDash", "name": "DD"})
        txs.append({"date": str(d), "amount_in": -500, "category": "TRANSFER_OUT", "merchant": "Me", "name": "Me"})
    sp = insights.spending(txs, TODAY - timedelta(days=29), TODAY, txs)
    assert sp["total"] == 900 and sp["normal_total"] == 300
    assert sp["up_from_normal"][0]["change_pct"] == 200
    assert [h["merchant"] for h in sp["repeat_small_purchases"]] == ["DoorDash", "Cafe"]


def test_demo_detection_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("DEBTBOT_DB", str(tmp_path / "d.db"))
    monkeypatch.setenv("DEBTBOT_KEY", Fernet.generate_key().decode())
    cfg = Config()
    svc = Service(cfg, Store(cfg.db_path, cfg.db_key), today_fn=lambda: TODAY)
    svc.refresh()
    ov = svc.recurring_overview()
    assert ov["income"] and ov["next_payday"] > str(TODAY)
    assert {"Mortgage", "Verizon"} <= {b["name"] for b in ov["bills_from_checking"]}
    assert "Chase Card Autopay" not in {b["name"] for b in ov["bills_from_checking"]}
    assert svc.subscriptions()["monthly_total"] > 100
    ev = svc.recurring_events()
    assert any(e["type"] == "price_up" and e["name"] == "Adobe" for e in ev)
    assert svc.recurring_events() == []      # each reported once
    # flag to cancel, then it charges again
    adobe = next(s for s in svc.store.streams() if s["name"] == "Adobe")
    svc.set_stream_kind(adobe["id"], "cancel")
    svc.store.set_stream(adobe["id"], last_date=str(TODAY))
    assert [e["type"] for e in svc.recurring_events()] == ["still_charging"]
    # a deposit from the detected payer counts as a paycheck
    chk = svc.checking_account()
    svc.store.add_transaction("p1", chk["id"], TODAY, "ACME CORP PAYROLL DIR DEP", 4150, merchant="ACME CORP PAYROLL",
                              category="INCOME")
    svc.store.add_transaction("p2", chk["id"], TODAY, "Venmo cashout", 80, category="TRANSFER_IN")
    assert [t["id"] for t in svc.new_paychecks()] == ["p1"]


def test_migrates_old_database(tmp_path):
    path = tmp_path / "old.db"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE transactions (id TEXT PRIMARY KEY, account_id TEXT, date TEXT, name TEXT, "
               "amount_in REAL, created_at TEXT, processed INTEGER DEFAULT 0)")
    db.execute("INSERT INTO transactions(id,account_id,date,name,amount_in) VALUES ('a','c','2026-10-01','x',5)")
    db.commit()
    db.close()
    s = Store(path, None)
    s.add_transaction("b", "c", "2026-10-02", "y", -3, merchant="Y", category="FOOD_AND_DRINK")
    cols = {r[1] for r in s.db.execute("PRAGMA table_info(transactions)")}
    assert {"merchant", "category", "detailed", "pending"} <= cols
    assert len(s.db.execute("SELECT * FROM transactions").fetchall()) == 2
