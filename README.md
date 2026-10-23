# Debt Agent

**To run it 24/7 in the cloud, see [DEPLOY.md](DEPLOY.md)** after trying it here.

When a paycheck lands, this agent texts you on Telegram with a plan: bills and your
safety buffer are set aside first, then card minimums, then extra money to the
costliest debt. Each card has a button that opens its payment page. You pay, tap
**Paid**, and it keeps track.

It **cannot move money**. It reads your accounts (read-only through Plaid), does the
math in plain Python, and Claude only explains results when you chat with it.

---

## 1. Run it on demo data (about 15 minutes)

You need Python 3.11 or newer ([python.org/downloads](https://www.python.org/downloads/)).

**Open a terminal in this folder**, then:

```bash
# Mac / Linux
python3 -m venv .venv
source .venv/bin/activate

# Windows (PowerShell)
py -m venv .venv
.venv\Scripts\Activate.ps1

pip install -r requirements.txt
```

**Create your Telegram bot**

1. In Telegram, message **@BotFather** and send `/newbot`. Pick a name. It gives you a token.
2. Copy `.env.example` to a new file named `.env`.
3. Paste the token after `TELEGRAM_BOT_TOKEN=`.
4. Make an encryption key and paste it after `DEBTBOT_KEY=`:
   ```bash
   python -m debtbot.cli genkey
   ```

**Start it**

```bash
python -m debtbot.bot
```

Message your bot anything. Because `TELEGRAM_USER_ID` is still empty, it replies with
your user ID. Put that number in `.env` after `TELEGRAM_USER_ID=`, stop the bot
(Ctrl+C), and start it again. From now on it only answers you.

**Try it:** send `/testpay 4200`. You'll get a payday plan with pay links and
Approve / Adjust / Skip. Approve it, tap each **✓ Paid**, then send `/status`.

## 2. Turn on chat (optional, recommended)

Get an API key at [console.anthropic.com](https://console.anthropic.com), put it after
`ANTHROPIC_API_KEY=` in `.env`, and restart. Now you can ask things like:

- "What's due this week?"
- "If I put an extra $1,000 on Capital One, when am I debt-free?"
- "Avalanche vs snowball with $600 a month extra?"
- "Make a plan with $1,500 on Discover and the rest to Chase."
- "Raise my buffer to $1,500." (you confirm with a button)

Without a key, everything else still works; use the commands below.

## 3. Make it yours: `config.yaml`

Open `config.yaml` and set:

- **income**: your pay schedule and what your paycheck deposit is called
  (e.g. `match: "PAYROLL"`). For owner draws, use `schedule: variable` and a rule
  matching the transfer from the business account.
- **bills**: recurring bills paid from checking (mortgage, utilities, insurance).
  These are always reserved before any card gets extra money.
- **settings**: `buffer`, `strategy` (avalanche or snowball).
- **accounts**: for each card, the payment page link (`pay_url`), any 0% promo end
  date, and `exclude: true` for business cards you want kept out.
- **manual_accounts**: loans Plaid can't see. Update the balance from a statement
  every month or two; in between, the agent estimates it.

Restart the bot after editing.

## 4. Plaid Sandbox (fake bank, real Plaid plumbing)

1. Sign up at [dashboard.plaid.com](https://dashboard.plaid.com/signup). Copy your
   client ID and **Sandbox** secret into `.env`. Keep `PLAID_ENV=sandbox`.
2. In `config.yaml`, set `mode: plaid`.
3. Link Plaid's fake bank, either from Telegram with `/link` (log in with
   `user_good` / `pass_good`) or from the terminal:
   ```bash
   python -m debtbot.cli link-sandbox
   ```
4. Start the bot. `/status` shows Plaid's test checking account, credit card,
   student loan and mortgage. Use `/testpay 4200` to trigger a plan.

Plaid Sandbox paychecks don't arrive on their own, so `/testpay` is how you test
the payday flow there.

## 5. Real accounts (after testing)

1. In the Plaid dashboard, request **Production** access for **Transactions** and
   **Liabilities**. Approval can take days to a couple of weeks.
2. Put your Production secret in `.env` and set `PLAID_ENV=production`.
   Delete `debtbot.db` first so sandbox data doesn't mix with real data.
3. Connect each bank: send `/link` in Telegram, tap the button, and log in
   through Plaid's window. Repeat for each bank. (On your own computer without
   the bot running, `python scripts/link_accounts.py` does the same.)
4. Start the bot. It checks for new deposits every 15 minutes, and the first sync
   ignores old history so you won't get alerts for past paychecks.

Some banks (Chase, Capital One and other big ones) use their own login page
(OAuth), which needs a redirect address registered with Plaid. DEPLOY.md covers
it. If one still won't connect, add it as a `manual_accounts` entry for now.

## Commands

| Command | What it does |
| --- | --- |
| `/status` | Balances, rates, minimums, due dates |
| `/plan` | Plan from what's in checking right now |
| `/testpay 4200` | Pretend a paycheck landed |
| `/compare 500` | Avalanche vs snowball with $500/month extra |
| `/strategy snowball` | Switch strategy |
| `/buffer 1500` | Change the safety buffer |
| `/sync` | Pull fresh bank data now |
| `/link` | Connect a bank from your phone |
| `/bills` | Recurring bills from checking and your detected pay |
| `/subs` | Subscriptions, monthly and yearly cost, keep or flag to cancel |
| `/spending 30` | Spending by category vs your normal, repeat small purchases |

**Adjust** on a plan lets you type extra amounts, e.g. `Discover 1500, Amex 200`.
Minimums are always kept.

**Every day at 9 AM** it reminds you about approved payments not yet marked paid,
minimums due within 3 days that aren't in a plan, and 0% promos ending in
90, 60, 30 or 14 days.

Command-line versions (no Telegram needed):

```bash
python -m debtbot.cli status
python -m debtbot.cli plan --deposit 4200
python -m debtbot.cli compare --extra 600
```

## What it learns from your transactions

Once your checking and cards are connected, Plaid spots everything that repeats:

- **Pay:** your paycheck deposits, so it knows when the next one is due and
  recognizes it when it lands (`income: schedule: auto` in `config.yaml`).
- **Bills from checking:** mortgage, utilities, insurance, phone. These are set
  aside before any card gets extra money. Credit-card autopays are recognized
  and not counted twice.
- **Subscriptions** on any account: cost per month and year, price increases,
  overlapping services (three streaming apps), and a 🚩 flag-to-cancel list that
  warns you if a flagged one charges again.
- **Spending:** by category against your usual, plus small purchases that add up.
  A recap arrives every Sunday at 6 PM.

The first time it sees your accounts it sends one summary. After that it tells
you about each new recurring charge or price increase once. Tap the buttons to
correct anything, or say it in chat ("Verizon isn't a bill").

When you chat about spending, Claude sees merchant names, amounts and dates for
the period you ask about (never account numbers or bank logins).

## How the plan is built

1. Start with the checking balance (after the paycheck).
2. Set aside bills due on or before your next payday.
3. Set aside your buffer. It is never touched.
4. Pay every minimum due on or before your next payday, soonest first.
5. Send the rest to the target debt: 0% promos ending within 90 days first
   (deferred-interest ones before regular ones), then highest APR (avalanche) or
   smallest balance (snowball).
6. If the leftover is under $50, it carries to next payday.

The math is in `debtbot/engine.py` and covered by tests (`pytest`).

## Keeping it running

On your computer it only runs while the terminal is open. Follow
[DEPLOY.md](DEPLOY.md) to run it on Railway around the clock, managed entirely
from your phone.

## Safety

- Read-only: only Plaid's Transactions and Liabilities products. No transfer
  product, so the code has no way to move money.
- Bank tokens are encrypted in `debtbot.db` with your `DEBTBOT_KEY`. Keep `.env`
  and `debtbot.db` private, and back up the key: without it the tokens can't be read.
- The bot ignores everyone except your Telegram user ID.
- Claude only sees balances, rates and dates through tools, never tokens or
  account numbers.
- Every plan, approval, payment and settings change is logged in the `audit` table.

## Files

```
debtbot/engine.py      payoff math (plans, ordering, simulations)
debtbot/service.py     ties data, math and bank sync together
debtbot/bot.py         Telegram bot, alerts, buttons, reminders
debtbot/assistant.py   Claude chat with read-only tools
debtbot/plaid_sync.py  Plaid accounts, liabilities, transactions
debtbot/store.py       SQLite storage, encrypted tokens, audit log
debtbot/web.py         one-time bank-linking page behind /link
debtbot/config.py      config.yaml, account settings, demo data
scripts/link_accounts.py  connect banks from your computer instead of /link
Dockerfile, railway.json  cloud deployment (see DEPLOY.md)
```
