"""How plans and summaries read on your phone (Telegram HTML)."""
from __future__ import annotations

import json
from datetime import date
from html import escape as e


def money(x: float) -> str:
    return f"${x:,.2f}" if abs(x - round(x)) > 0.004 else f"${x:,.0f}"


def day(s: str | None) -> str:
    if not s:
        return ""
    d = date.fromisoformat(s)
    return f"{d:%b} {d.day}"


def plan_text(plan_row: dict, payments: list[dict], title: str | None = None) -> str:
    s = json.loads(plan_row["summary"])
    total = sum((p["minimum"] or 0) + (p["extra"] or 0) for p in payments)
    bills_total = sum(a for _, a, _ in s["bills"])
    head = title or (f"💰 <b>Paycheck landed: {money(s['deposit'])}</b>" if s["deposit"] else "📋 <b>Payment plan</b>")
    out = [head, ""]
    out.append(f"Checking: {money(s['checking'])}")
    out.append(f"Bills before {day(s['next_payday'])}: {money(bills_total)} · Buffer kept: {money(s['buffer'])}")
    if s["bills"]:
        out.append("<i>" + e(", ".join(f"{n} {money(a)} ({day(d)})" for n, a, d in s["bills"])) + "</i>")
    out.append("")
    if payments:
        out.append("<b>Suggested payments:</b>")
        for p in payments:
            amt = (p["minimum"] or 0) + (p["extra"] or 0)
            parts = []
            if p["minimum"]:
                parts.append(f"{money(p['minimum'])} minimum, due {day(p['due_date'])}")
            if p["extra"]:
                why = (p.get("reason") or "").split("extra: ")[-1]
                parts.append(f"{money(p['extra'])} extra" + (f": {e(why)}" if why and not why.startswith("minimum") else ""))
            out.append(f"• <b>{e(p['name'])}: {money(amt)}</b> ({'; '.join(parts)})")
        out.append("")
        out.append(f"Total: {money(total)} · Left in checking: {money(s['checking'] - total)}")
    else:
        out.append("Nothing to pay from this one.")
    if s.get("carried"):
        out.append(f"Carried to next payday: {money(s['carried'])}")
    for n in s.get("notes") or []:
        out.append(f"⚠️ {e(n)}")
    return "\n".join(out)


def status_text(snap: dict) -> str:
    out = [f"<b>Total debt: {money(snap['total_debt'])}</b>"]
    if snap["checking_balance"] is not None:
        out.append(f"Checking: {money(snap['checking_balance'])} (buffer {money(snap['buffer'])})")
    out.append(f"Strategy: {snap['strategy']} · Next payday: {day(snap['next_payday'])}")
    out.append("")
    for d in snap["debts"]:
        rate = f"{d['apr_now']:.2f}%"
        if d["promo_end"] and d["apr_now"] == 0:
            rate = f"0% until {day(d['promo_end'])}, then {d['apr']:.2f}%"
        due = f", min {money(d['min_payment'])} due {day(d['due_date'])}" if d["due_date"] else ""
        src = "" if d["source"] in ("plaid", "demo") else f" <i>({d['source']}, {d['updated']})</i>"
        out.append(f"• <b>{e(d['name'])}</b>: {money(d['balance'])} at {rate}{due}{src}")
    return "\n".join(out)


def compare_text(cmp: dict, extra: float) -> str:
    a, s = cmp["avalanche"], cmp["snowball"]
    saved = s["total_interest"] - a["total_interest"]
    lines = [f"<b>Paying minimums + {money(extra)}/month extra</b>", ""]
    for name, r in (("Avalanche", a), ("Snowball", s)):
        lines.append(f"<b>{name}</b>: debt-free {r['debt_free']} ({r['months']} months), "
                     f"interest {money(r['total_interest'])}")
        lines.append("   Order: " + e(" → ".join(r["payoff_order"])))
    lines.append("")
    if saved > 1:
        lines.append(f"Avalanche saves {money(saved)} in interest.")
    elif saved < -1:
        lines.append(f"Snowball saves {money(-saved)} in interest here.")
    else:
        lines.append("They come out about the same.")
    return "\n".join(lines)
