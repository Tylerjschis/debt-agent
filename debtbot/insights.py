"""Recurring charges, subscriptions and spending patterns.

Plaid detects recurring "streams" (the same merchant charging on a schedule)
and labels every transaction with a category. This module turns that into:
bills to reserve before payday, a subscription list with costs, and a
spending summary. All plain arithmetic; no AI.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

from .engine import Bill, _advance

PER_MONTH = {"weekly": 52 / 12, "biweekly": 26 / 12, "semi_monthly": 2.0, "monthly": 1.0, "annually": 1 / 12}

# Never counted as spending (money moving between your own accounts, debt payments, pay)
NOT_SPENDING = {"TRANSFER_IN", "TRANSFER_OUT", "LOAN_PAYMENTS", "INCOME"}

# Things that look like subscriptions even when they cost more than the usual cutoff
SUBSCRIPTION_HINTS = (
    "ENTERTAINMENT_TV_AND_MOVIES", "ENTERTAINMENT_MUSIC_AND_AUDIO", "ENTERTAINMENT_VIDEO_GAMES",
    "PERSONAL_CARE_GYMS_AND_FITNESS_CENTERS", "GENERAL_SERVICES_OTHER_GENERAL_SERVICES",
    "GENERAL_MERCHANDISE_ONLINE_MARKETPLACES",
)
# Clearly bills, never subscriptions
BILL_CATEGORIES = {"RENT_AND_UTILITIES", "LOAN_PAYMENTS", "GOVERNMENT_AND_NON_PROFIT", "MEDICAL"}
BILL_DETAILED = ("GENERAL_SERVICES_INSURANCE", "GENERAL_SERVICES_CHILDCARE", "GENERAL_SERVICES_EDUCATION",
                 "LOAN_PAYMENTS_MORTGAGE_PAYMENT", "LOAN_PAYMENTS_CAR_PAYMENT")


def monthly(amount: float, frequency: str | None) -> float:
    return amount * PER_MONTH.get((frequency or "monthly").lower(), 1.0)


def pretty_category(cat: str | None) -> str:
    if not cat:
        return "Other"
    return cat.replace("_AND_", " & ").replace("_", " ").title()


def auto_kind(s: dict) -> str:
    """Best guess for a stream: income | transfer | bill | subscription | other_in."""
    cat, det = (s.get("category") or "").upper(), (s.get("detailed") or "").upper()
    if s["direction"] == "in":
        return "income" if cat == "INCOME" else "other_in"
    if det == "LOAN_PAYMENTS_CREDIT_CARD_PAYMENT" or cat in ("TRANSFER_OUT", "TRANSFER_IN"):
        return "transfer"
    if cat in BILL_CATEGORIES or det.startswith(BILL_DETAILED):
        return "bill"
    if det.startswith(SUBSCRIPTION_HINTS) or (s.get("avg_amount") or 0) <= 100:
        return "subscription"
    return "bill"


def kind(s: dict) -> str:
    """Your choice if you made one, otherwise the automatic guess."""
    return s.get("kind") or auto_kind(s)


def next_due(s: dict, today: date) -> date | None:
    d = s.get("next_date") or s.get("last_date")
    if not d:
        return None
    d = date.fromisoformat(d)
    guard = 0
    while d < today and guard < 400:
        d, guard = _advance(d, s.get("frequency") or "monthly"), guard + 1
    return d


def checking_bills(streams: list[dict], today: date) -> list[Bill]:
    """Recurring charges paid straight from checking: reserved before any card gets extra."""
    out = []
    for s in streams:
        if s["direction"] != "out" or s.get("account_type") != "depository":
            continue
        if kind(s) in ("transfer", "ignore"):
            continue
        nd = next_due(s, today)
        if nd:
            amount = max(s.get("avg_amount") or 0, s.get("last_amount") or 0)
            out.append(Bill(s.get("merchant") or s["name"], round(amount, 2), next_date=nd,
                            frequency=s.get("frequency") or "monthly"))
    return out


def income_streams(streams: list[dict]) -> list[dict]:
    inc = [s for s in streams if s["direction"] == "in" and kind(s) == "income"]
    return sorted(inc, key=lambda s: -monthly(s.get("avg_amount") or 0, s.get("frequency")))


def subscriptions(streams: list[dict], today: date) -> dict:
    subs = [s for s in streams if s["direction"] == "out" and kind(s) in ("subscription", "cancel")]
    rows = []
    for s in subs:
        avg, last = s.get("avg_amount") or 0, s.get("last_amount") or 0
        rows.append({
            "id": s["id"], "name": s.get("merchant") or s["name"], "amount": round(last or avg, 2),
            "frequency": s.get("frequency") or "monthly",
            "monthly": round(monthly(last or avg, s.get("frequency")), 2),
            "yearly": round(monthly(last or avg, s.get("frequency")) * 12, 2),
            "account": s.get("account_name"), "next": str(next_due(s, today) or ""),
            "price_up": round(last - avg, 2) if last > avg * 1.05 and last - avg >= 1 else 0,
            "category": s.get("detailed") or s.get("category"),
            "flagged": s.get("kind") == "cancel",
        })
    rows.sort(key=lambda r: -r["monthly"])
    by_cat = defaultdict(list)
    for r in rows:
        if r["category"]:
            by_cat[r["category"]].append(r["name"])
    # Several services of the same kind (three streaming apps, two music apps)
    overlaps = {pretty_category(c.replace("ENTERTAINMENT_", "").replace("PERSONAL_CARE_", "")): names
                for c, names in by_cat.items()
                if len(names) > 1 and c.startswith(("ENTERTAINMENT_", "PERSONAL_CARE_GYMS"))}
    return {
        "subscriptions": rows,
        "monthly_total": round(sum(r["monthly"] for r in rows), 2),
        "yearly_total": round(sum(r["yearly"] for r in rows), 2),
        "flagged_to_cancel": [r["name"] for r in rows if r["flagged"]],
        "overlaps": overlaps,
    }


def _spend_rows(txs: list[dict]) -> list[dict]:
    return [t for t in txs if (t.get("amount_in") or 0) < 0 and (t.get("category") or "") not in NOT_SPENDING]


def spending(txs: list[dict], start: date, end: date, history: list[dict]) -> dict:
    """Spending between start and end, compared with the average of the three
    equal-length periods before it (from `history`)."""
    days = (end - start).days + 1
    cur = _spend_rows([t for t in txs if start <= date.fromisoformat(t["date"]) <= end])
    by_cat: dict[str, float] = defaultdict(float)
    by_merchant: dict[str, list[float]] = defaultdict(list)
    for t in cur:
        by_cat[t.get("category") or "OTHER"] += -t["amount_in"]
        by_merchant[t.get("merchant") or t["name"]].append(-t["amount_in"])

    prev: dict[str, float] = defaultdict(float)
    periods = 0
    for k in range(1, 4):
        p_end = start - timedelta(days=1 + (k - 1) * days)
        p_start = p_end - timedelta(days=days - 1)
        rows = _spend_rows([t for t in history if p_start <= date.fromisoformat(t["date"]) <= p_end])
        if rows:
            periods += 1
        for t in rows:
            prev[t.get("category") or "OTHER"] += -t["amount_in"]
    normal = {c: v / periods for c, v in prev.items()} if periods else {}

    cats = []
    for c, v in sorted(by_cat.items(), key=lambda x: -x[1]):
        n = normal.get(c)
        cats.append({"category": pretty_category(c), "key": c, "spent": round(v, 2),
                     "normal": round(n, 2) if n is not None else None,
                     "change_pct": round((v - n) / n * 100) if n else None})
    habits = sorted(
        [{"merchant": m, "count": len(a), "total": round(sum(a), 2), "average": round(sum(a) / len(a), 2)}
         for m, a in by_merchant.items() if len(a) >= 4 and sum(a) / len(a) <= 40],
        key=lambda h: -h["total"])
    top = sorted(([m, round(sum(a), 2), len(a)] for m, a in by_merchant.items()), key=lambda x: -x[1])[:5]
    everyday = [t for t in cur if (t.get("category") or "") not in BILL_CATEGORIES
                and not (t.get("detailed") or "").startswith(BILL_DETAILED)]
    biggest = max(everyday, key=lambda t: -t["amount_in"], default=None)
    total = round(sum(by_cat.values()), 2)
    normal_total = round(sum(normal.values()), 2) if normal else None
    return {
        "start": str(start), "end": str(end), "days": days, "total": total,
        "normal_total": normal_total, "periods_compared": periods,
        "categories": cats,
        # Bills move with due-date timing, so only everyday spending is flagged
        "up_from_normal": [c for c in cats if c["key"] not in BILL_CATEGORIES and c["change_pct"] is not None
                           and (c["normal"] or 0) >= 50 and c["change_pct"] >= 25
                           and c["spent"] - (c["normal"] or 0) >= 50],
        "repeat_small_purchases": habits[:5],
        "top_merchants": [{"merchant": m, "total": v, "count": n} for m, v, n in top],
        "biggest": {"merchant": biggest.get("merchant") or biggest["name"], "amount": round(-biggest["amount_in"], 2),
                    "date": biggest["date"]} if biggest else None,
    }
