"""Telegram bot: payday alerts, approvals, reminders and chat.

Run:  python -m debtbot.bot
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date, time
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters,
)

from .config import Config
from .engine import match_debt
from .messages import compare_text, day, money, plan_text, status_text
from .service import Service
from .store import Store

log = logging.getLogger("debtbot")

HELP = """<b>Debt Agent</b>
I watch your accounts, and when a paycheck lands I send a plan: bills and your buffer first, then minimums, then extra to the costliest debt. Tap a card's link to pay it, then tap Paid.

/status – balances, rates, due dates
/plan – plan from what's in checking now
/testpay 4200 – pretend a paycheck landed (testing)
/compare 500 – avalanche vs snowball with $500/mo extra
/strategy avalanche|snowball
/buffer 1500 – change your safety buffer
/sync – pull fresh bank data now
/link – connect a bank from your phone

Or just ask: "what if I put $1,000 on Capital One?" """


class Bot:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.store = Store(cfg.db_path, cfg.db_key)
        self.svc = Service(cfg, self.store)
        self.ai = None
        if cfg.anthropic_key:
            from .assistant import Assistant
            self.ai = Assistant(self.svc, cfg.anthropic_key, cfg.model)
        self.pending_settings: dict[int, dict] = {}
        self._next_settings_id = 1
        from .web import LinkServer
        self.web = LinkServer(self.svc, cfg.port, cfg.plaid_redirect_uri)

    # -- sending ---------------------------------------------------------------
    def _plan_keyboard(self, plan_id: int, payments: list[dict], status: str) -> InlineKeyboardMarkup | None:
        rows = []
        if status == "proposed":
            for p in payments:
                if p.get("pay_url"):
                    amt = (p["minimum"] or 0) + (p["extra"] or 0)
                    rows.append([InlineKeyboardButton(f"{p['name']}: {money(amt)} ↗", url=p["pay_url"])])
            rows.append([
                InlineKeyboardButton("✅ Approve", callback_data=f"ap:{plan_id}"),
                InlineKeyboardButton("✏️ Adjust", callback_data=f"aj:{plan_id}"),
                InlineKeyboardButton("⏭ Skip", callback_data=f"sk:{plan_id}"),
            ])
        elif status == "approved":
            for p in payments:
                if p["status"] == "paid":
                    continue
                amt = (p["minimum"] or 0) + (p["extra"] or 0)
                row = []
                if p.get("pay_url"):
                    row.append(InlineKeyboardButton(f"Pay {p['name']} {money(amt)} ↗", url=p["pay_url"]))
                row.append(InlineKeyboardButton("✓ Paid" if row else f"✓ Paid {p['name']}",
                                                callback_data=f"pd:{p['id']}"))
                rows.append(row)
        return InlineKeyboardMarkup(rows) if rows else None

    def _plan_message(self, plan_id: int) -> tuple[str, InlineKeyboardMarkup | None]:
        plan = self.store.plan(plan_id)
        pays = self.store.payments(plan_id)
        text = plan_text(plan, pays)
        status = plan["status"]
        if status == "approved":
            left = [p for p in pays if p["status"] != "paid"]
            text += ("\n\n✅ <b>Approved.</b> Pay each one with its link, then tap Paid."
                     if left else "\n\n🎉 <b>All paid.</b> Nice work.")
        elif status == "skipped":
            text += "\n\n⏭ Skipped. The money stays in checking."
        elif status == "replaced":
            text += "\n\n<i>Replaced by a newer plan.</i>"
        return text, self._plan_keyboard(plan_id, pays, status)

    async def send_plan(self, app_or_ctx, plan_id: int):
        self.store.replace_open_plans(plan_id)
        text, kb = self._plan_message(plan_id)
        await app_or_ctx.bot.send_message(self.cfg.owner_id, text, parse_mode=ParseMode.HTML,
                                          reply_markup=kb, disable_web_page_preview=True)

    # -- background jobs --------------------------------------------------------
    async def poll_job(self, ctx: ContextTypes.DEFAULT_TYPE):
        try:
            broken = await asyncio.to_thread(self.svc.refresh)
        except Exception:  # noqa: BLE001
            log.exception("refresh failed")
            return
        today = date.today().isoformat()
        for name in broken:
            key = f"broken_notified:{name}"
            if self.store.kv_get(key) != today:
                self.store.kv_set(key, today)
                await ctx.bot.send_message(
                    self.cfg.owner_id,
                    f"🔌 {name} needs you to log in again. Until then its numbers may be out of date.\n"
                    "Send /link and log in to it again to reconnect.",
                )
        for tx in await asyncio.to_thread(self.svc.new_paychecks):
            await self.payday(ctx, tx["amount_in"], tx["id"])

    async def payday(self, ctx, amount: float, tx_id: str | None):
        _, pid = await asyncio.to_thread(self.svc.build_plan, amount, tx_id)
        await self.send_plan(ctx, pid)

    async def reminder_job(self, ctx: ContextTypes.DEFAULT_TYPE):
        for r in await asyncio.to_thread(self.svc.reminders):
            if r["kind"] == "unpaid":
                amt = (r["minimum"] or 0) + (r["extra"] or 0)
                rows = []
                if r.get("pay_url"):
                    rows.append(InlineKeyboardButton(f"Pay {money(amt)} ↗", url=r["pay_url"]))
                rows.append(InlineKeyboardButton("✓ Paid", callback_data=f"pd:{r['id']}"))
                await ctx.bot.send_message(
                    self.cfg.owner_id,
                    f"⏰ {r['name']}: {money(amt)} is approved but not marked paid. Due {day(r['due_date'])}.",
                    reply_markup=InlineKeyboardMarkup([rows]))
            elif r["kind"] == "unplanned":
                rows = [InlineKeyboardButton("Make a plan", callback_data="pl:0")]
                if r.get("pay_url"):
                    rows.insert(0, InlineKeyboardButton(f"Pay {money(r['minimum'])} ↗", url=r["pay_url"]))
                await ctx.bot.send_message(
                    self.cfg.owner_id,
                    f"⏰ {r['name']}: {money(r['minimum'])} minimum due {day(r['due_date'])} isn't in any plan yet.",
                    reply_markup=InlineKeyboardMarkup([rows]))
            elif r["kind"] == "promo":
                extra = " Deferred interest: if any balance is left, back interest can be charged." \
                    if r["deferred_interest"] else ""
                await ctx.bot.send_message(
                    self.cfg.owner_id,
                    f"📅 {r['name']}: 0% promo ends {day(r['promo_end'])} with {money(r['balance'])} left.{extra}")

    # -- commands ----------------------------------------------------------------
    async def cmd_help(self, update: Update, ctx):
        await update.message.reply_text(HELP, parse_mode=ParseMode.HTML)

    async def cmd_status(self, update: Update, ctx):
        await asyncio.to_thread(self.svc.refresh)
        await update.message.reply_text(status_text(self.svc.snapshot()), parse_mode=ParseMode.HTML)

    async def cmd_plan(self, update: Update, ctx):
        await asyncio.to_thread(self.svc.refresh)
        _, pid = await asyncio.to_thread(self.svc.build_plan)
        await self.send_plan(ctx, pid)

    async def cmd_testpay(self, update: Update, ctx):
        amount = _num(ctx.args[0]) if ctx.args else 4200.0
        if not amount or amount <= 0:
            return await update.message.reply_text("Usage: /testpay 4200")
        await asyncio.to_thread(self.svc.refresh)
        tx = await asyncio.to_thread(self.svc.fake_paycheck, amount)
        await self.payday(ctx, amount, tx["id"])

    async def cmd_compare(self, update: Update, ctx):
        extra = _num(ctx.args[0]) if ctx.args else 500.0
        res = await asyncio.to_thread(self.svc.compare, extra or 0)
        await update.message.reply_text(compare_text(res, extra or 0), parse_mode=ParseMode.HTML)

    async def cmd_strategy(self, update: Update, ctx):
        s = (ctx.args[0].lower() if ctx.args else "")
        if s not in ("avalanche", "snowball"):
            return await update.message.reply_text("Usage: /strategy avalanche  or  /strategy snowball")
        self.svc.update_settings(strategy=s)
        await update.message.reply_text(f"Strategy set to {s}.")

    async def cmd_buffer(self, update: Update, ctx):
        amount = _num(ctx.args[0]) if ctx.args else None
        if amount is None or amount < 0:
            return await update.message.reply_text("Usage: /buffer 1500")
        self.svc.update_settings(buffer=amount)
        await update.message.reply_text(f"Buffer set to {money(amount)}. Checking won't be planned below that.")

    async def cmd_link(self, update: Update, ctx):
        if self.svc.plaid is None:
            return await update.message.reply_text(
                "Bank linking needs mode: plaid in config.yaml and Plaid keys in your settings.")
        url = f"{self.cfg.public_url}/link?t={self.web.new_token()}"
        note = "\nSandbox login: user_good / pass_good" if self.cfg.plaid_env == "sandbox" else ""
        await update.message.reply_text(
            "Tap to connect a bank. This link works for 15 minutes and only for you." + note,
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("Connect a bank ↗", url=url)]]))

    async def cmd_sync(self, update: Update, ctx):
        broken = await asyncio.to_thread(self.svc.refresh)
        msg = f"Synced {len(self.store.accounts())} accounts."
        if broken:
            msg += " Needs re-login: " + ", ".join(broken)
        await update.message.reply_text(msg)

    # -- buttons -------------------------------------------------------------------
    async def on_button(self, update: Update, ctx):
        q = update.callback_query
        kind, _, arg = (q.data or "").partition(":")
        await q.answer()
        if kind in ("ap", "sk", "aj"):
            pid = int(arg)
            plan = self.store.plan(pid)
            if not plan or plan["status"] != "proposed":
                return await q.edit_message_reply_markup(None)
            if kind == "aj":
                ctx.user_data["adjusting"] = pid
                return await q.message.reply_text(
                    "What should change? Type the extra amounts, like:\n"
                    "Discover 1500, Capital One 1785\n"
                    "Minimums are kept automatically." + ("\nOr just describe it in plain English." if self.ai else ""))
            self.store.set_plan_status(pid, "approved" if kind == "ap" else "skipped")
            text, kb = self._plan_message(pid)
            return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                             disable_web_page_preview=True)
        if kind == "pd":
            pay = await asyncio.to_thread(self.svc.mark_paid, int(arg))
            if not pay:
                return
            plan = self.store.plan(pay["plan_id"])
            if plan and plan["status"] == "approved" and q.message.text and "Suggested payments" in q.message.text:
                text, kb = self._plan_message(pay["plan_id"])
                return await q.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                                 disable_web_page_preview=True)
            amt = (pay["minimum"] or 0) + (pay["extra"] or 0)
            return await q.edit_message_text(f"✓ {pay['name']}: {money(amt)} marked paid.")
        if kind == "pl":
            _, pid = await asyncio.to_thread(self.svc.build_plan)
            return await self.send_plan(ctx, pid)
        if kind in ("cs", "cx"):
            change = self.pending_settings.pop(int(arg), None)
            if not change:
                return await q.edit_message_reply_markup(None)
            if kind == "cs":
                self.svc.update_settings(**change)
                return await q.edit_message_text("Updated: " + ", ".join(f"{k} = {v}" for k, v in change.items()))
            return await q.edit_message_text("Okay, nothing changed.")

    # -- free text -------------------------------------------------------------------
    async def on_text(self, update: Update, ctx):
        text = (update.message.text or "").strip()
        adjusting = ctx.user_data.pop("adjusting", None)
        if adjusting:
            overrides = self._parse_overrides(text)
            if overrides:
                self.store.set_plan_status(adjusting, "replaced")
                _, pid = await asyncio.to_thread(self.svc.build_plan, 0.0, None, overrides)
                return await self.send_plan(ctx, pid)
            if not self.ai:
                ctx.user_data["adjusting"] = adjusting
                return await update.message.reply_text(
                    "I couldn't match those to your accounts. Try: Discover 1500, Amex 200")
            text = f"(I'm adjusting the payment plan you just sent.) {text}"
        if not self.ai:
            return await update.message.reply_text(
                "Chat needs an ANTHROPIC_API_KEY in .env. Meanwhile, try /status, /plan or /compare 500.")
        await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
        history = ctx.chat_data.setdefault("history", [])
        try:
            reply, actions = await asyncio.to_thread(self.ai.ask, list(history), text)
        except Exception as e:  # noqa: BLE001
            log.exception("assistant failed")
            return await update.message.reply_text(f"Sorry, chat hit an error: {str(e)[:200]}")
        history += [{"role": "user", "content": text}, {"role": "assistant", "content": reply}]
        del history[:-12]
        await update.message.reply_text(reply)
        for a in actions:
            if a["type"] == "plan":
                await self.send_plan(ctx, a["plan_id"])
            elif a["type"] == "confirm_settings" and a["changes"]:
                sid = self._next_settings_id
                self._next_settings_id += 1
                self.pending_settings[sid] = a["changes"]
                desc = ", ".join(f"{k} → {v}" for k, v in a["changes"].items())
                await update.message.reply_text(
                    f"Change settings: {desc}?",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton("Yes, change it", callback_data=f"cs:{sid}"),
                        InlineKeyboardButton("No", callback_data=f"cx:{sid}"),
                    ]]))

    def _parse_overrides(self, text: str) -> dict[str, float] | None:
        """'Discover 1500, Capital One $1,785' -> {'Discover It': 1500, ...}; None unless every part matches."""
        debts = self.store.debts()
        out: dict[str, float] = {}
        parts = [p for p in re.split(r",|;|\band\b|\n", text) if p.strip()]
        for part in parts:
            m = re.fullmatch(r"\s*(?:put\s+)?(.+?)\s*[=:]?\s*\$?([\d,]+(?:\.\d+)?)\s*", part, re.I) or \
                re.fullmatch(r"\s*(?:put\s+)?\$?([\d,]+(?:\.\d+)?)\s+(?:on|to)\s+(.+?)\s*", part, re.I)
            if not m:
                return None
            a, b = m.groups()
            name, amount = (a, b) if not re.fullmatch(r"[\d,.]+", a) else (b, a)
            d = match_debt(debts, name)
            if not d:
                return None
            out[d.name] = float(amount.replace(",", ""))
        return out or None

    # -- wiring --------------------------------------------------------------------
    def build(self) -> Application:
        cfg = self.cfg
        app = Application.builder().token(cfg.telegram_token).post_init(self._post_init).build()

        # Setup mode: tell you your ID, then hand you an encryption key. Nothing else runs.
        if not cfg.owner_id:
            async def whoami(update: Update, ctx):
                await update.message.reply_text(
                    f"Your Telegram user ID is {update.effective_user.id}.\n\n"
                    "Add it as TELEGRAM_USER_ID in your settings (Railway Variables or .env). "
                    "I'll restart and continue setup.")
            app.add_handler(MessageHandler(filters.ALL, whoami))
            log.warning("TELEGRAM_USER_ID not set: the bot will only tell people their ID.")
            return app
        if not cfg.db_key:
            from cryptography.fernet import Fernet
            self._new_key = Fernet.generate_key().decode()

            async def give_key(update: Update, ctx):
                await self._send_key(ctx.bot)
            app.add_handler(MessageHandler(filters.User(user_id=cfg.owner_id), give_key))
            log.warning("DEBTBOT_KEY not set: sending the owner a new key and waiting.")
            return app

        me = filters.User(user_id=cfg.owner_id)
        for name, fn in [("start", self.cmd_help), ("help", self.cmd_help), ("status", self.cmd_status),
                         ("plan", self.cmd_plan), ("testpay", self.cmd_testpay), ("compare", self.cmd_compare),
                         ("strategy", self.cmd_strategy), ("buffer", self.cmd_buffer), ("sync", self.cmd_sync),
                         ("link", self.cmd_link)]:
            app.add_handler(CommandHandler(name, fn, filters=me))
        app.add_handler(CallbackQueryHandler(self._guard_button))
        app.add_handler(MessageHandler(me & filters.TEXT & ~filters.COMMAND, self.on_text))

        jq = app.job_queue
        jq.run_repeating(self.poll_job, interval=cfg.check_minutes * 60, first=5)
        jq.run_daily(self.reminder_job, time=time(9, 0, tzinfo=ZoneInfo(cfg.timezone)))
        app.add_error_handler(self._on_error)
        return app

    async def _guard_button(self, update: Update, ctx):
        if update.effective_user and update.effective_user.id == self.cfg.owner_id:
            return await self.on_button(update, ctx)
        await update.callback_query.answer("Not your bot.")

    async def _send_key(self, bot):
        await bot.send_message(
            self.cfg.owner_id,
            "🔑 Last setup step. Here's your new encryption key:\n\n"
            f"<code>{self._new_key}</code>\n\n"
            "1. Tap the key to copy it.\n"
            "2. Save it in your password manager.\n"
            "3. Add it as DEBTBOT_KEY in your settings (Railway Variables or .env).\n"
            "4. Then delete this message.\n\n"
            "It protects your bank connections. If you lose it, you'd re-link your banks.",
            parse_mode=ParseMode.HTML)

    async def _post_init(self, app: Application):
        if not self.cfg.owner_id:
            return
        if not self.cfg.db_key:
            await self._send_key(app.bot)
            return
        await app.bot.set_my_commands([
            BotCommand("status", "Balances, rates, due dates"),
            BotCommand("plan", "Plan from what's in checking now"),
            BotCommand("testpay", "Pretend a paycheck landed"),
            BotCommand("compare", "Avalanche vs snowball"),
            BotCommand("strategy", "Set avalanche or snowball"),
            BotCommand("buffer", "Set your safety buffer"),
            BotCommand("sync", "Pull fresh bank data"),
            BotCommand("link", "Connect a bank"),
            BotCommand("help", "What I can do"),
        ])
        await asyncio.to_thread(self.svc.refresh)
        if self.cfg.owner_id:
            mode = "demo data" if self.cfg.mode == "demo" else f"Plaid ({self.cfg.plaid_env})"
            await app.bot.send_message(self.cfg.owner_id,
                                       f"🟢 Debt Agent is running with {mode}. Send /help to see what I can do.")

    async def _on_error(self, update, ctx):
        log.exception("Unhandled error", exc_info=ctx.error)


def _num(s: str) -> float | None:
    try:
        return float(s.replace("$", "").replace(",", ""))
    except (ValueError, AttributeError):
        return None


def main():
    logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    cfg = Config()
    if not cfg.telegram_token:
        raise SystemExit("TELEGRAM_BOT_TOKEN is missing from .env")
    bot = Bot(cfg)
    app = bot.build()
    bot.web.start()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
