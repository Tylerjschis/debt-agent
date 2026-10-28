"""Payoff engine: all the money math lives here.

Plain, deterministic Python. No AI, no network. Every dollar figure the
agent shows you comes from these functions, so they are covered by tests.
"""
from __future__ import annotations

import calendar
from dataclasses import dataclass, field
from datetime import date, timedelta

CENT = 0.005  # anything smaller than half a cent counts as zero


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------

@dataclass
class Debt:
    id: str
    name: str
    balance: float
    apr: float                       # regular annual rate, in percent (24.99)
    min_payment: float
    due_date: date | None = None
    promo_end: date | None = None    # 0% promo ends on this date
    deferred_interest: bool = False  # back interest lands if promo balance isn't cleared
    pay_url: str | None = None
    source: str = "plaid"            # plaid | manual | statement | demo
    updated_at: str | None = None

    def effective_apr(self, on: date) -> float:
        """Rate actually charged on a given day (0 while a promo is active)."""
        if self.promo_end and on < self.promo_end:
            return 0.0
        return self.apr


@dataclass
class Bill:
    """A recurring charge paid from checking.

    Either `due_day` (day of the month, from config.yaml) or `next_date` plus
    `frequency` (detected from your transactions) says when it hits.
    """
    name: str
    amount: float
    due_day: int | None = None
    next_date: date | None = None
    frequency: str = "monthly"  # weekly | biweekly | semi_monthly | monthly | annually


STEP_DAYS = {"weekly": 7, "biweekly": 14, "semi_monthly": 15}


def _advance(d: date, frequency: str) -> date:
    f = (frequency or "monthly").lower()
    if f in STEP_DAYS:
        return d + timedelta(days=STEP_DAYS[f])
    if f == "annually":
        return _clamp_day(d.year + 1, d.month, d.day)
    y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
    return _clamp_day(y, m, d.day)


@dataclass
class Settings:
    buffer: float = 1000.0
    strategy: str = "avalanche"      # avalanche | snowball
    carry_threshold: float = 50.0    # don't split less than this; carry it forward
    promo_window_days: int = 90      # promo ending within this many days jumps the line


@dataclass
class PlanLine:
    debt_id: str
    name: str
    minimum: float
    extra: float
    due_date: date | None
    pay_url: str | None
    reason: str

    @property
    def total(self) -> float:
        return round(self.minimum + self.extra, 2)


@dataclass
class Plan:
    deposit: float
    checking: float
    next_payday: date
    bills_due: list[tuple[str, float, date]]
    buffer: float
    lines: list[PlanLine] = field(default_factory=list)
    shortfall: float = 0.0
    carried: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def bills_total(self) -> float:
        return round(sum(a for _, a, _ in self.bills_due), 2)

    @property
    def payments_total(self) -> float:
        return round(sum(l.total for l in self.lines), 2)

    @property
    def left_in_checking(self) -> float:
        return round(self.checking - self.payments_total, 2)


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def _clamp_day(year: int, month: int, day: int) -> date:
    last = calendar.monthrange(year, month)[1]
    return date(year, month, min(day, last))


def next_on_day(day: int, on_or_after: date) -> date:
    """Next date falling on `day` of the month (clamped for short months)."""
    d = _clamp_day(on_or_after.year, on_or_after.month, day)
    if d >= on_or_after:
        return d
    y, m = (on_or_after.year + 1, 1) if on_or_after.month == 12 else (on_or_after.year, on_or_after.month + 1)
    return _clamp_day(y, m, day)


def next_payday(income: dict, today: date) -> date:
    """When the next paycheck after today is expected.

    income keys: schedule = biweekly | weekly | semimonthly | monthly | variable,
    anchor_date (biweekly/weekly), days (semimonthly), day (monthly),
    horizon_days (variable).
    """
    schedule = (income.get("schedule") or "variable").lower()
    if schedule in ("biweekly", "weekly"):
        step = 14 if schedule == "biweekly" else 7
        anchor = income.get("anchor_date")
        anchor = date.fromisoformat(str(anchor)) if anchor else today
        d = anchor
        while d <= today:
            d += timedelta(days=step)
        while d - timedelta(days=step) > today:
            d -= timedelta(days=step)
        return d
    if schedule == "semimonthly":
        days = sorted(income.get("days") or [1, 15])
        return min(next_on_day(dd, today + timedelta(days=1)) for dd in days)
    if schedule == "monthly":
        return next_on_day(int(income.get("day") or 1), today + timedelta(days=1))
    # variable / owner draws: plan over a fixed window
    return today + timedelta(days=int(income.get("horizon_days") or 30))


def bills_due_between(bills: list[Bill], start: date, end: date) -> list[tuple[str, float, date]]:
    """Every bill occurrence with start <= due <= end."""
    out = []
    for b in bills:
        if b.next_date:
            d, guard = b.next_date, 0
            while d < start and guard < 400:
                d, guard = _advance(d, b.frequency), guard + 1
            while d <= end:
                out.append((b.name, round(b.amount, 2), d))
                d = _advance(d, b.frequency)
            continue
        if not b.due_day:
            continue
        d = next_on_day(b.due_day, start)
        while d <= end:
            out.append((b.name, round(b.amount, 2), d))
            y, m = (d.year + 1, 1) if d.month == 12 else (d.year, d.month + 1)
            d = _clamp_day(y, m, b.due_day)
    return sorted(out, key=lambda x: x[2])


# ---------------------------------------------------------------------------
# Ordering
# ---------------------------------------------------------------------------

def target_order(debts: list[Debt], settings: Settings, today: date) -> list[Debt]:
    """Which debt gets extra money first.

    1. Deferred-interest promos ending soon (back interest can land all at once)
    2. Other 0% promos ending soon (clear them before the full rate hits)
    3. Everything else by strategy: avalanche = highest effective APR first,
       snowball = smallest balance first.
    """
    window_end = today + timedelta(days=settings.promo_window_days)

    def key(d: Debt):
        ending_soon = d.promo_end is not None and today < d.promo_end <= window_end
        if ending_soon and d.deferred_interest:
            return (0, d.promo_end.toordinal(), 0.0)
        if ending_soon:
            return (1, d.promo_end.toordinal(), 0.0)
        if settings.strategy == "snowball":
            return (2, d.balance, -d.effective_apr(today))
        return (2, -d.effective_apr(today), d.balance)

    return sorted([d for d in debts if d.balance > CENT], key=key)


def _reason(d: Debt, settings: Settings, today: date, first: bool = True) -> str:
    window_end = today + timedelta(days=settings.promo_window_days)
    if d.promo_end and today < d.promo_end <= window_end:
        kind = "deferred-interest promo" if d.deferred_interest else "0% promo"
        return f"{kind} ends {d.promo_end:%b} {d.promo_end.day}"
    if settings.strategy == "snowball":
        return "smallest balance" if first else "next smallest balance"
    return f"{d.effective_apr(today):.2f}% APR" + (", highest" if first else "")


# ---------------------------------------------------------------------------
# Paycheck plan
# ---------------------------------------------------------------------------

def match_debt(debts: list[Debt], name: str) -> Debt | None:
    """Find a debt by (part of) its name, case-insensitive."""
    n = name.strip().lower()
    exact = [d for d in debts if d.name.lower() == n]
    if exact:
        return exact[0]
    partial = [d for d in debts if n in d.name.lower()]
    return partial[0] if len(partial) == 1 else None


def plan_paycheck(
    *,
    checking: float,
    deposit: float,
    debts: list[Debt],
    bills: list[Bill],
    settings: Settings,
    today: date,
    next_pay: date,
    already_covered: set[str] | None = None,
    overrides: dict[str, float] | None = None,
) -> Plan:
    """Split the money in checking.

    Order: bills due before next payday -> buffer -> minimums due before
    next payday -> extra to the target debt(s). `already_covered` holds debt
    ids whose current minimum is already approved or paid. `overrides` maps a
    debt name to a custom extra amount and replaces the automatic extra split.
    """
    already_covered = already_covered or set()
    plan = Plan(
        deposit=round(deposit, 2),
        checking=round(checking, 2),
        next_payday=next_pay,
        bills_due=bills_due_between(bills, today, next_pay),
        buffer=round(settings.buffer, 2),
    )
    remaining = round(checking - plan.bills_total - settings.buffer, 2)
    if remaining < 0:
        plan.notes.append(
            f"Checking is ${-remaining:,.2f} short of covering upcoming bills plus your buffer."
        )

    lines: dict[str, PlanLine] = {}
    owed = {d.id: d.balance for d in debts}

    def line_for(d: Debt, reason: str) -> PlanLine:
        if d.id not in lines:
            lines[d.id] = PlanLine(d.id, d.name, 0.0, 0.0, d.due_date, d.pay_url, reason)
        return lines[d.id]

    # Minimums due on or before next payday (overdue ones too), soonest first.
    due = [
        d for d in debts
        if d.balance > CENT and d.min_payment > CENT and d.due_date
        and d.due_date <= next_pay and d.id not in already_covered
    ]
    for d in sorted(due, key=lambda x: x.due_date):
        need = round(min(d.min_payment, d.balance), 2)
        pay = round(max(0.0, min(need, remaining)), 2)
        if pay < need:
            plan.shortfall = round(plan.shortfall + need - pay, 2)
        if pay > CENT:
            ln = line_for(d, f"minimum, due {d.due_date:%b} {d.due_date.day}")
            ln.minimum = pay
            owed[d.id] = round(owed[d.id] - pay, 2)
            remaining = round(remaining - pay, 2)
    if plan.shortfall > CENT:
        plan.notes.append(
            f"Not enough to cover every minimum: ${plan.shortfall:,.2f} short. Minimums are listed soonest-due first."
        )

    # Extra money.
    if remaining > CENT and overrides:
        for name, amount in overrides.items():
            d = match_debt(debts, name)
            if d is None:
                plan.notes.append(f"Couldn't find an account matching '{name}'.")
                continue
            pay = round(max(0.0, min(float(amount), remaining, owed[d.id])), 2)
            if pay < float(amount) - CENT:
                plan.notes.append(f"{d.name}: capped at ${pay:,.2f} (balance or available cash).")
            if pay > CENT:
                line_for(d, "your choice").extra = round(line_for(d, "your choice").extra + pay, 2)
                owed[d.id] = round(owed[d.id] - pay, 2)
                remaining = round(remaining - pay, 2)
        if remaining > CENT:
            plan.carried = remaining
    elif remaining >= settings.carry_threshold:
        promo_first = False
        for d in target_order(debts, settings, today):
            if remaining <= CENT:
                break
            room = owed[d.id]
            if room <= CENT:
                continue
            pay = round(min(room, remaining), 2)
            is_promo = bool(d.promo_end and today < d.promo_end <= today + timedelta(days=settings.promo_window_days))
            why = _reason(d, settings, today, first=is_promo or not promo_first)
            promo_first = promo_first or not is_promo
            ln = line_for(d, why)
            if ln.minimum > CENT:
                ln.reason = f"{ln.reason}; extra: {why}"
            ln.extra = round(ln.extra + pay, 2)
            owed[d.id] = round(owed[d.id] - pay, 2)
            remaining = round(remaining - pay, 2)
    elif remaining > CENT:
        plan.carried = remaining
        plan.notes.append(
            f"${remaining:,.2f} extra is under your ${settings.carry_threshold:,.0f} threshold, so it carries to next payday."
        )

    plan.lines = sorted(lines.values(), key=lambda l: (l.minimum <= CENT, l.due_date or date.max))
    return plan


# ---------------------------------------------------------------------------
# Payoff simulation
# ---------------------------------------------------------------------------

@dataclass
class PayoffResult:
    strategy: str
    months: int
    debt_free: date | None
    total_interest: float
    payoff_month: dict[str, int]
    capped: bool = False  # True if it never pays off within the limit
    balances: list[float] = field(default_factory=list)  # total owed at the start and after each month


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return _clamp_day(d.year + m // 12, m % 12 + 1, d.day)


def simulate_payoff(
    debts: list[Debt],
    *,
    monthly_extra: float = 0.0,
    strategy: str = "avalanche",
    today: date,
    lump_sum: float = 0.0,
    lump_target: str | None = None,
    promo_window_days: int = 90,
    max_months: int = 600,
) -> PayoffResult:
    """Month-by-month payoff with the minimums plus `monthly_extra`.

    Budget stays constant: when a debt is paid off, its minimum rolls into
    the extra pool. Minimum payments are held at today's amounts (real cards
    lower them as balances fall, so real payoff can be slightly slower if you
    only ever pay minimums). Deferred-interest back charges are not modeled.
    """
    s = Settings(strategy=strategy, promo_window_days=promo_window_days)
    sim = [Debt(**{**d.__dict__}) for d in debts if d.balance > CENT]
    budget = sum(d.min_payment for d in sim) + monthly_extra
    total_interest = 0.0
    payoff: dict[str, int] = {}

    if lump_sum > CENT:
        pool = lump_sum
        targets = target_order(sim, s, today)
        if lump_target:
            t = match_debt(sim, lump_target)
            if t:
                targets = [t] + [x for x in targets if x.id != t.id]
        for d in targets:
            pay = min(pool, d.balance)
            d.balance -= pay
            pool -= pay
            if pool <= CENT:
                break

    month = 0
    series = [round(sum(d.balance for d in sim), 2)]
    while any(d.balance > CENT for d in sim) and month < max_months:
        month += 1
        on = _add_months(today, month)
        for d in sim:
            if d.balance > CENT:
                interest = d.balance * d.effective_apr(on) / 1200
                d.balance += interest
                total_interest += interest
        pool = budget
        for d in sim:
            if d.balance > CENT:
                pay = min(d.min_payment, d.balance, pool)
                d.balance -= pay
                pool -= pay
        for d in target_order(sim, s, on):
            if pool <= CENT:
                break
            pay = min(pool, d.balance)
            d.balance -= pay
            pool -= pay
        for d in sim:
            if d.balance <= CENT and d.name not in payoff:
                payoff[d.name] = month
        series.append(round(sum(max(d.balance, 0) for d in sim), 2))

    capped = any(d.balance > CENT for d in sim)
    return PayoffResult(
        strategy=strategy,
        months=month,
        debt_free=None if capped else _add_months(today, month),
        total_interest=round(total_interest, 2),
        payoff_month=payoff,
        capped=capped,
        balances=series,
    )
