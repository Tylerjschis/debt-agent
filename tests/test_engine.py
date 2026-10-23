from datetime import date, timedelta

from debtbot.engine import (
    Bill, Debt, Settings, bills_due_between, next_on_day, next_payday,
    plan_paycheck, simulate_payoff, target_order,
)

TODAY = date(2026, 10, 23)


def debts():
    return [
        Debt("chase", "Chase Sapphire", 4850, 24.99, 95, TODAY + timedelta(days=5)),
        Debt("cap1", "Capital One", 2140, 29.99, 60, TODAY + timedelta(days=12)),
        Debt("disc", "Discover It", 3600, 22.99, 72, TODAY + timedelta(days=18),
             promo_end=TODAY + timedelta(days=60)),
        Debt("amex", "Amex Blue Cash", 980, 19.99, 40, TODAY + timedelta(days=25)),
    ]


def test_next_on_day_clamps_short_months():
    assert next_on_day(31, date(2026, 2, 10)) == date(2026, 2, 28)
    assert next_on_day(5, date(2026, 12, 20)) == date(2027, 1, 5)


def test_next_payday_schedules():
    assert next_payday({"schedule": "biweekly", "anchor_date": "2026-10-09"}, TODAY) == date(2026, 11, 6)
    assert next_payday({"schedule": "semimonthly", "days": [1, 15]}, TODAY) == date(2026, 11, 1)
    assert next_payday({"schedule": "monthly", "day": 23}, TODAY) == date(2026, 11, 23)
    assert next_payday({"schedule": "variable", "horizon_days": 30}, TODAY) == TODAY + timedelta(days=30)


def test_bills_window():
    bills = [Bill("Mortgage", 1850, 1), Bill("Phone", 120, 24), Bill("Gym", 40, 20)]
    got = bills_due_between(bills, TODAY, date(2026, 11, 6))
    assert [b[0] for b in got] == ["Phone", "Mortgage"]


def test_promo_ending_jumps_the_line():
    order = target_order(debts(), Settings(), TODAY)
    assert order[0].id == "disc"           # 0% ends in 60 days
    assert [d.id for d in order[1:]] == ["cap1", "chase", "amex"]  # then by APR


def test_snowball_order():
    order = target_order(debts(), Settings(strategy="snowball", promo_window_days=0), TODAY)
    assert [d.id for d in order] == ["amex", "cap1", "disc", "chase"]


def test_plan_reserves_bills_and_buffer_then_minimums_then_extra():
    bills = [Bill("Mortgage", 1850, 1)]
    plan = plan_paycheck(
        checking=6500, deposit=4200, debts=debts(), bills=bills,
        settings=Settings(buffer=1000), today=TODAY, next_pay=date(2026, 11, 6),
    )
    # available = 6500 - 1850 - 1000 = 3650; minimums due by Nov 6: chase 95, cap1 60
    by = {l.debt_id: l for l in plan.lines}
    assert by["chase"].minimum == 95 and by["cap1"].minimum == 60
    assert by["disc"].extra == 3650 - 155  # all extra goes to the promo card first
    assert by["cap1"].extra == 0
    assert plan.left_in_checking == 2850  # bills + buffer stay put
    assert plan.shortfall == 0


def test_plan_never_touches_buffer_when_short():
    plan = plan_paycheck(
        checking=1100, deposit=900, debts=debts(), bills=[], settings=Settings(buffer=1000),
        today=TODAY, next_pay=date(2026, 11, 6),
    )
    assert plan.payments_total == 100
    assert plan.shortfall == 55
    assert plan.left_in_checking == 1000


def test_already_covered_minimum_skipped():
    plan = plan_paycheck(
        checking=1300, deposit=0, debts=debts(), bills=[], settings=Settings(buffer=1000, carry_threshold=500),
        today=TODAY, next_pay=date(2026, 11, 6), already_covered={"chase"},
    )
    assert [l.debt_id for l in plan.lines] == ["cap1"]
    assert plan.carried == 240


def test_overrides():
    plan = plan_paycheck(
        checking=3000, deposit=2000, debts=debts(), bills=[], settings=Settings(buffer=1000),
        today=TODAY, next_pay=date(2026, 11, 6), overrides={"amex": 500, "nope": 10},
    )
    by = {l.debt_id: l for l in plan.lines}
    assert by["amex"].extra == 500
    assert plan.carried == round(2000 - 155 - 500, 2)
    assert any("nope" in n for n in plan.notes)


def test_simulation_avalanche_beats_snowball_on_interest():
    a = simulate_payoff(debts(), monthly_extra=600, strategy="avalanche", today=TODAY)
    s = simulate_payoff(debts(), monthly_extra=600, strategy="snowball", today=TODAY, promo_window_days=0)
    assert not a.capped and not s.capped
    assert a.total_interest <= s.total_interest
    assert set(a.payoff_month) == {"Chase Sapphire", "Capital One", "Discover It", "Amex Blue Cash"}


def test_simulation_detects_never_paid_off():
    bad = [Debt("x", "Tiny min", 10000, 30, 50)]
    r = simulate_payoff(bad, today=TODAY, max_months=120)
    assert r.capped and r.debt_free is None


def test_lump_sum_lowers_interest():
    base = simulate_payoff(debts(), monthly_extra=300, today=TODAY)
    lump = simulate_payoff(debts(), monthly_extra=300, today=TODAY, lump_sum=1000, lump_target="Capital")
    assert lump.total_interest < base.total_interest
