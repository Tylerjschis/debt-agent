"""Plain-English chat, powered by Claude with read-only tools.

Claude never does the math and never sees bank tokens or account numbers:
it calls the tools below, which run the same code as everything else, and
puts the results into words. No tool can move money.
"""
from __future__ import annotations

import json
import logging
from datetime import date

import anthropic

from .service import Service

log = logging.getLogger(__name__)

SYSTEM = """You are a personal debt-payoff assistant chatting with one person over Telegram.

Rules:
- Every number you state must come from a tool result in this conversation. Never do money math in your head; call simulate_payoff, compare_strategies or propose_payment_plan instead.
- You cannot move money. When someone wants to pay something, propose a plan; they pay it themselves from the links and tap Paid.
- Settings changes (buffer, strategy) go through request_settings_change, which asks them to confirm with a button.
- You are not a licensed financial advisor. Explain tradeoffs factually; don't recommend specific investments.
- For spending questions use get_spending, get_subscriptions, find_transactions. Be direct and specific about habits worth changing (with the dollar amounts and what it would free up for debt), without lecturing.
- Replies are read on a phone: short, plain text, no markdown tables or headers. Use dollar amounts and dates.
Today is {today}."""

TOOLS = [
    {
        "name": "get_overview",
        "description": "Current debts (balance, APR now and after any promo, minimum, due date, data source), checking balance, buffer, strategy, next payday, total debt.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_upcoming",
        "description": "Bills and debt minimums due in the next N days.",
        "input_schema": {"type": "object", "properties": {"days": {"type": "integer", "default": 14}}},
    },
    {
        "name": "simulate_payoff",
        "description": "Project the debt-free date and total interest if the person pays all minimums plus monthly_extra each month, optionally with a one-time lump sum now (to lump_target, or by strategy).",
        "input_schema": {
            "type": "object",
            "properties": {
                "monthly_extra": {"type": "number", "default": 0},
                "lump_sum": {"type": "number", "default": 0},
                "lump_target": {"type": "string", "description": "Part of an account name"},
                "strategy": {"type": "string", "enum": ["avalanche", "snowball"]},
            },
        },
    },
    {
        "name": "compare_strategies",
        "description": "Avalanche vs snowball: debt-free date, months, total interest and payoff order for the same monthly extra.",
        "input_schema": {"type": "object", "properties": {"monthly_extra": {"type": "number", "default": 500}},
                         "required": ["monthly_extra"]},
    },
    {
        "name": "get_recurring",
        "description": "Recurring bills paid from checking (detected from transactions), detected pay and next payday, monthly bills total.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_subscriptions",
        "description": "Subscriptions (recurring charges on any account) with monthly/yearly cost, price increases, overlapping services, and which are flagged to cancel.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "get_spending",
        "description": "Spending by category for the last N days compared with the person's normal, categories that are up, repeat small purchases, top merchants.",
        "input_schema": {"type": "object", "properties": {"days": {"type": "integer", "default": 30}}},
    },
    {
        "name": "find_transactions",
        "description": "Search recent transactions by merchant, description or category text (empty text = all). Returns date, merchant, amount, category, account.",
        "input_schema": {"type": "object", "properties": {"text": {"type": "string"},
                                                          "days": {"type": "integer", "default": 30}}},
    },
    {
        "name": "mark_recurring",
        "description": "Correct how a recurring charge or deposit is treated, matched by part of its name. choice: bill (reserve before payday), subscription, cancel (flag to cancel), ignore (not a real recurring charge), income (this is my pay), other_in (deposit that isn't pay).",
        "input_schema": {"type": "object", "properties": {
            "name": {"type": "string"},
            "choice": {"type": "string", "enum": ["bill", "subscription", "cancel", "ignore", "income", "other_in"]}},
            "required": ["name", "choice"]},
    },
    {
        "name": "propose_payment_plan",
        "description": "Build a payment plan from the money in checking now and send it to the person with Approve/Adjust/Skip buttons. Minimums due before next payday are always included automatically. Leave extra_by_account empty to split extra money by their strategy, or pass account-name -> extra dollar amount to choose the extras yourself (replaces the automatic split). Bills and the buffer are always protected.",
        "input_schema": {
            "type": "object",
            "properties": {
                "extra_by_account": {"type": "object", "additionalProperties": {"type": "number"}},
            },
        },
    },
    {
        "name": "mark_paid",
        "description": "Record that the person paid the approved payment for an account (matched by part of its name).",
        "input_schema": {"type": "object", "properties": {"account_name": {"type": "string"}},
                         "required": ["account_name"]},
    },
    {
        "name": "request_settings_change",
        "description": "Ask the person to confirm a settings change (they tap a button). buffer = dollars checking never drops below.",
        "input_schema": {
            "type": "object",
            "properties": {
                "buffer": {"type": "number"},
                "strategy": {"type": "string", "enum": ["avalanche", "snowball"]},
                "carry_threshold": {"type": "number"},
            },
        },
    },
]


class Assistant:
    def __init__(self, svc: Service, api_key: str, model: str):
        self.svc = svc
        self.client = anthropic.Anthropic(api_key=api_key)
        self.model = model

    def _run_tool(self, name: str, args: dict, actions: list) -> dict:
        svc = self.svc
        if name == "get_overview":
            return svc.snapshot()
        if name == "get_upcoming":
            return svc.upcoming(int(args.get("days") or 14))
        if name == "simulate_payoff":
            return svc.simulate(float(args.get("monthly_extra") or 0), float(args.get("lump_sum") or 0),
                                args.get("lump_target"), args.get("strategy"))
        if name == "compare_strategies":
            return svc.compare(float(args.get("monthly_extra") or 0))
        if name == "get_recurring":
            return svc.recurring_overview()
        if name == "get_subscriptions":
            return svc.subscriptions()
        if name == "get_spending":
            sp = svc.spending(max(7, min(int(args.get("days") or 30), 120)))
            for c in sp["categories"]:
                c.pop("key", None)
            return sp
        if name == "find_transactions":
            return {"transactions": svc.find_transactions(args.get("text") or "", int(args.get("days") or 30))}
        if name == "mark_recurring":
            want = args["name"].lower()
            hits = [s for s in svc.store.streams(active_only=False)
                    if want in f"{s.get('merchant') or ''} {s['name']}".lower()]
            if len(hits) != 1:
                return {"error": f"{len(hits)} recurring items match '{args['name']}'; be more specific.",
                        "matches": [s.get("merchant") or s["name"] for s in hits][:10]}
            s = svc.set_stream_kind(hits[0]["id"], args["choice"])
            return {"updated": s.get("merchant") or s["name"], "now": args["choice"]}
        if name == "propose_payment_plan":
            overrides = args.get("extra_by_account") or None
            plan, pid = svc.build_plan(overrides=overrides)
            actions.append({"type": "plan", "plan_id": pid})
            return {
                "sent_to_person": True,
                "payments": [{"account": l.name, "minimum": l.minimum, "extra": l.extra} for l in plan.lines],
                "total": plan.payments_total, "left_in_checking": plan.left_in_checking,
                "carried": plan.carried, "notes": plan.notes,
            }
        if name == "mark_paid":
            want = args["account_name"].lower()
            for p in reversed(svc.store.open_payments()):
                if want in p["name"].lower():
                    svc.mark_paid(p["id"])
                    return {"marked_paid": p["name"], "amount": (p["minimum"] or 0) + (p["extra"] or 0)}
            return {"error": "No approved, unpaid payment matches that account."}
        if name == "request_settings_change":
            changes = {k: v for k, v in args.items() if v is not None}
            actions.append({"type": "confirm_settings", "changes": changes})
            return {"status": "confirmation button sent; not changed until they tap it", "changes": changes}
        return {"error": f"unknown tool {name}"}

    def ask(self, history: list[dict], text: str) -> tuple[str, list[dict]]:
        """Returns (reply, actions). `history` holds prior plain-text turns."""
        messages = history + [{"role": "user", "content": text}]
        actions: list[dict] = []
        system = SYSTEM.format(today=date.today().isoformat())
        for _ in range(8):  # tool-call rounds
            resp = self.client.messages.create(model=self.model, max_tokens=1024, system=system,
                                               tools=TOOLS, messages=messages)
            if resp.stop_reason != "tool_use":
                reply = "".join(b.text for b in resp.content if b.type == "text").strip()
                return reply or "Done.", actions
            messages.append({"role": "assistant", "content": resp.content})
            results = []
            for b in resp.content:
                if b.type == "tool_use":
                    try:
                        out = self._run_tool(b.name, dict(b.input or {}), actions)
                    except Exception as e:  # noqa: BLE001
                        log.exception("tool %s failed", b.name)
                        out = {"error": str(e)[:300]}
                    results.append({"type": "tool_result", "tool_use_id": b.id,
                                    "content": json.dumps(out, default=str)})
            messages.append({"role": "user", "content": results})
        return "That took too many steps; try asking a simpler question.", actions
