"""Monthly budget: spending by category against limits you set.

Categories are Plaid's spending categories (Food & Drink, Transportation...).
Money moving between your own accounts, pay, and debt payments are not
spending; debt payments are shown on their own line.
"""
from __future__ import annotations

import calendar
import math
from collections import defaultdict
from datetime import date, timedelta

from .insights import NOT_SPENDING, pretty_category


def month_bounds(month: str | None, today: date) -> tuple[date, date]:
    """'2026-10' -> (Oct 1, Oct 31). Defaults to the current month."""
    y, m = (int(month[:4]), int(month[5:7])) if month else (today.year, today.month)
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def _prev_month(d: date) -> date:
    return (d.replace(day=1) - timedelta(days=1)).replace(day=1)


def spend_by_category(txs: list[dict], start: date, end: date) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for t in txs:
        if (t.get("amount_in") or 0) >= 0 or (t.get("category") or "") in NOT_SPENDING:
            continue
        if start <= date.fromisoformat(t["date"]) <= end:
            out[t.get("category") or "OTHER"] += -t["amount_in"]
    return dict(out)


def suggest(txs: list[dict], today: date, months: int = 3) -> dict[str, float]:
    """Average of the last `months` full months per category, rounded up to $10.
    Categories under $10 a month are left out."""
    first = today.replace(day=1)
    totals: dict[str, float] = defaultdict(float)
    seen = 0
    m = first
    for _ in range(months):
        m = _prev_month(m)
        start, end = month_bounds(m.isoformat()[:7], today)
        month_spend = spend_by_category(txs, start, end)
        if month_spend:
            seen += 1
        for c, v in month_spend.items():
            totals[c] += v
    if not seen:
        return {}
    return {c: float(math.ceil(v / seen / 10) * 10) for c, v in totals.items() if v / seen >= 10}


def status(txs: list[dict], limits: dict[str, float], today: date, month: str | None = None) -> dict:
    start, end = month_bounds(month, today)
    days = (end - start).days + 1
    day_n = min(max((today - start).days + 1, 0), days) if today >= start else 0
    spent = spend_by_category(txs, start, end)
    income = sum(t["amount_in"] for t in txs if (t.get("amount_in") or 0) > 0
                 and t.get("category") == "INCOME" and start <= date.fromisoformat(t["date"]) <= end)
    debt_paid = sum(-t["amount_in"] for t in txs if (t.get("amount_in") or 0) < 0
                    and t.get("category") == "LOAN_PAYMENTS" and start <= date.fromisoformat(t["date"]) <= end)

    rows = []
    for c in sorted(set(limits) | set(spent), key=lambda c: -(limits.get(c) or 0) - spent.get(c, 0) / 1e6):
        lim, s = limits.get(c), round(spent.get(c, 0), 2)
        rows.append({
            "key": c, "category": pretty_category(c), "spent": s,
            "limit": lim, "left": round(lim - s, 2) if lim is not None else None,
            "pct": round(s / lim * 100) if lim else None,
            # On pace = spent no faster than the month is passing
            "status": None if lim is None else ("over" if s > lim else "ahead" if lim and s / lim > day_n / days + 0.1 else "ok"),
        })
    budgeted = [r for r in rows if r["limit"] is not None]
    total_limit = round(sum(r["limit"] for r in budgeted), 2)
    total_budgeted_spent = round(sum(r["spent"] for r in budgeted), 2)
    left = round(total_limit - total_budgeted_spent, 2)
    days_left = days - day_n + (1 if day_n else 0) if today <= end else 0
    return {
        "month": start.isoformat()[:7], "month_name": f"{start:%B}", "days": days, "day": day_n,
        "days_left": days_left,
        "income": round(income, 2), "spent": round(sum(spent.values()), 2), "debt_payments": round(debt_paid, 2),
        "budget_total": total_limit, "budgeted_spent": total_budgeted_spent, "left": left,
        "per_day_left": round(left / days_left, 2) if days_left and left > 0 else 0,
        "unbudgeted_spent": round(sum(r["spent"] for r in rows if r["limit"] is None), 2),
        "has_budget": bool(budgeted),
        "categories": rows,
    }
