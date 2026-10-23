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


FREQ = {"weekly": "/wk", "biweekly": "every 2 wks", "semi_monthly": "twice a month", "monthly": "/mo",
        "annually": "/yr"}


def bills_text(ov: dict) -> str:
    out = ["<b>Recurring from checking</b> (set aside before any card gets extra)", ""]
    for b in ov["bills_from_checking"]:
        nxt = b["next"] if not b["next"][:4].isdigit() else day(b["next"])
        out.append(f"• {e(b['name'])}: {money(b['amount'])} {FREQ.get(b['frequency'], '')}, next {nxt}")
    if not ov["bills_from_checking"]:
        out.append("None detected yet. They appear after a few weeks of transactions.")
    out += ["", f"About {money(ov['monthly_bills_total'])}/month."]
    if ov["income"]:
        i = ov["income"][0]
        out.append(f"💵 Pay: {e(i['name'])} about {money(i['amount'])} {FREQ.get(i['frequency'], '')}. "
                   f"Next expected {day(ov['next_payday'])}.")
    else:
        out.append("💵 Pay not detected yet; planning assumes the next 2 weeks.")
    out.append("\nWrong? Tap a charge in /subs, or tell me (\"Verizon isn't a bill\").")
    return "\n".join(out)


def subs_text(s: dict) -> str:
    rows = s["subscriptions"]
    if not rows:
        return "No subscriptions detected yet."
    out = [f"<b>Subscriptions: {money(s['monthly_total'])}/month · {money(s['yearly_total'])}/year</b>", ""]
    for r in rows:
        extra = f" ⬆️ up {money(r['price_up'])}" if r["price_up"] else ""
        flag = " 🚩" if r["flagged"] else ""
        unit = FREQ.get(r["frequency"], "")
        per = f"{money(r['amount'])}{unit}" + (f" ({money(r['monthly'])}/mo)" if r["frequency"] != "monthly" else "")
        out.append(f"• {e(r['name'])}: {per}{extra}{flag}")
    for kind_, names in s["overlaps"].items():
        out.append(f"\n💡 {len(names)} {e(kind_.lower())} services: {e(', '.join(names))}. Need all of them?")
    if s["flagged_to_cancel"]:
        out.append(f"\n🚩 To cancel: {e(', '.join(s['flagged_to_cancel']))}")
    out.append("\nTap one below to keep it or flag it to cancel.")
    return "\n".join(out)


def spending_text(sp: dict, title: str | None = None) -> str:
    out = [title or f"<b>Spending, last {sp['days']} days: {money(sp['total'])}</b>"]
    if sp["normal_total"]:
        diff = sp["total"] - sp["normal_total"]
        word = "more" if diff > 0 else "less"
        out.append(f"That's {money(abs(diff))} {word} than your usual {money(sp['normal_total'])}.")
    out.append("")
    for c in sp["categories"][:8]:
        ch = (f" ({'+' if c['change_pct'] > 0 else ''}{c['change_pct']}%)"
              if c["change_pct"] not in (None, 0) and (c["normal"] or 0) >= 25 else "")
        out.append(f"• {e(c['category'])}: {money(c['spent'])}{ch}")
    if sp["up_from_normal"]:
        out.append("")
        for c in sp["up_from_normal"]:
            out.append(f"📈 {e(c['category'])} is up {c['change_pct']}%: {money(c['spent'])} vs usual {money(c['normal'])}")
    if sp["repeat_small_purchases"]:
        out.append("")
        for h in sp["repeat_small_purchases"][:3]:
            out.append(f"🔁 {e(h['merchant'])}: {h['count']} times, {money(h['total'])} total (~{money(h['average'])} each)")
    if sp["biggest"]:
        b = sp["biggest"]
        out.append(f"\nBiggest: {e(b['merchant'])} {money(b['amount'])} on {day(b['date'])}")
    return "\n".join(out)


def recap_text(r: dict) -> str:
    sp = r["spending"]
    out = [spending_text(sp, f"🗓 <b>Your week: {money(sp['total'])} spent</b>")]
    if r["renewing_this_week"]:
        out.append("\nRenewing this week: " + e(", ".join(f"{s['name']} {money(s['amount'])}" for s in r["renewing_this_week"])))
    out.append(f"\nTotal debt: {money(r['total_debt'])}")
    return "\n".join(out)


def stream_event_text(ev: dict) -> str:
    name = e(ev.get("merchant") or ev["name"])
    amt = money(ev.get("last_amount") or ev.get("avg_amount") or 0)
    unit = FREQ.get(ev.get("frequency") or "", "")
    where = f" on {e(ev['account_name'])}" if ev.get("account_name") else ""
    if ev["type"] == "still_charging":
        return f"🚩 <b>{name}</b> charged {amt} again{where}, but you flagged it to cancel. Did the cancellation go through?"
    if ev["type"] == "price_up":
        return (f"⬆️ <b>{name}</b> went up: now {amt}{unit}, was about {money(ev.get('avg_amount') or 0)}{where}.")
    label = {"subscription": "New subscription", "bill": "New recurring bill", "income": "Recurring deposit"}.get(
        ev["kind"], "New recurring charge")
    return f"🔎 <b>{label}:</b> {name} {amt}{unit}{where}."
