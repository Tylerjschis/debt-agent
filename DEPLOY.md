# Run it 24/7 on Railway

After this, the agent runs in the cloud around the clock. You use it from
Telegram on your phone, connect banks with `/link`, and change settings by
editing `config.yaml` on GitHub, which you can do from your phone too. Your
computer can be off.

Takes about 20 minutes, mostly clicking. Railway's paid Hobby plan is about
$5 a month and this bot is small enough to fit inside it; check
[railway.com/pricing](https://railway.com/pricing) for current terms.

Railway's screens change from time to time; if a button is named a little
differently, look for the closest match.

---

## 1. Put the code in a private GitHub repo

1. Sign in at [github.com](https://github.com) (create a free account if needed).
2. Click **New repository**. Name it `debt-agent` and choose **Private**. Create it.
3. On the empty repo page, click **uploading an existing file**.
4. Unzip `debt-agent.zip` on your computer, open the `debt-agent` folder, select
   everything inside it, and drag it into the upload area. Click **Commit changes**.

Never upload your `.env` file or any `.db` file. The secrets go into Railway in step 3.

## 2. Create the Railway service

1. Go to [railway.com](https://railway.com) and sign in with GitHub.
2. **New Project → Deploy from GitHub repo**, pick `debt-agent`, and allow access
   to that repo when asked.
3. Railway finds the `Dockerfile` and starts building. The first deploy will
   fail or idle because secrets aren't set yet; that's expected.

## 3. Add a volume (where your data lives)

1. Open the service, then add a **Volume** (in the service's settings, or
   right-click the service on the project canvas → **Attach Volume**).
2. Set the **mount path** to `/data`.

Without the volume, your data would be wiped on every deploy.

## 4. Add your secrets

Open the service's **Variables** tab and add each one (same values as your `.env`):

| Variable | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | from @BotFather |
| `TELEGRAM_USER_ID` | your numeric Telegram ID |
| `DEBTBOT_KEY` | leave it out at first: the bot sends you one in Telegram to paste here |
| `ANTHROPIC_API_KEY` | optional, turns on chat |
| `PLAID_CLIENT_ID` | when you switch to `mode: plaid` |
| `PLAID_SECRET` | Sandbox secret first, Production later |
| `PLAID_ENV` | `sandbox` (later `production`) |

Save a copy of `DEBTBOT_KEY` in your password manager. Without it, saved bank
connections can't be read and you'd have to re-link every bank.

## 5. Give it a web address (for `/link`)

In the service's **Settings → Networking**, click **Generate Domain**. If it asks
for a port, enter `8080`. The bot picks up this address automatically, so
`/link` sends you a working URL.

## 6. Deploy and check

Railway redeploys when you change variables. When the logs show
`Link server listening` and you get **🟢 Debt Agent is running** in Telegram,
it's live.

**Stop the copy on your computer** if one is running (Ctrl+C). Two copies of the
same bot fight over Telegram messages.

---

## Day to day

- **Everything happens in Telegram.** Paycheck alerts, approvals, reminders, chat.
- **Connect a bank:** send `/link`, tap the button, log in. In Sandbox the
  login is `user_good` / `pass_good`.
- **Change settings** (bills, buffer, pay schedule, pay links): open
  `config.yaml` in your GitHub repo, tap the pencil icon, edit, and commit.
  Railway redeploys within a couple of minutes. This works from the GitHub
  mobile app or your phone's browser.
- **Quick changes from chat:** `/buffer 1500`, `/strategy snowball`.

## Moving from demo to real data

1. In `config.yaml`, set `mode: plaid`.
2. In Railway variables, add `DEBTBOT_DB` = `/data/live.db`. This starts a fresh
   database so demo or Sandbox data doesn't mix with real accounts.
3. Put your Plaid keys in the variables. Use `/link` for each bank.

Do the same again (a new `DEBTBOT_DB` name) when switching from Sandbox to
Production.

## Banks with their own login page (Chase, Capital One and others)

Many big banks send you to their own site to log in (OAuth). On a phone, Plaid
needs to know where to send you back:

1. In the Plaid dashboard, open **Developers → API → Allowed redirect URIs** and
   add `https://YOUR-RAILWAY-DOMAIN/oauth`.
2. In Railway variables, add `PLAID_REDIRECT_URI` with the same address.

## If something goes wrong

- **No 🟢 message:** check the service's **Deploy Logs**. A missing variable
  is named there.
- **"Conflict: terminated by other getUpdates request"** in the logs: another copy of
  the bot is running somewhere. Stop it.
- **Bot goes quiet:** Railway restarts it automatically after a crash. If it
  keeps crashing, the logs say why.
- **`/link` says the link expired:** links last 15 minutes. Send `/link` again.
- **`/link` sends a `localhost` address:** add a variable `PUBLIC_URL` set to
  your Railway domain (e.g. `https://debt-agent-production.up.railway.app`).
