/* Debt Agent dashboard. Plain JavaScript, no outside libraries.
   Everything shown comes from /api/*. Text from banks is always inserted as text,
   never as HTML. */
"use strict";

const view = document.getElementById("view");
const tabs = document.getElementById("tabs");
const state = { chatOn: false, spendDays: 30, extra: 300, txQuery: "" };

/* ---------- helpers ---------- */
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style") el.style.cssText = v; // CSSOM, allowed by the strict CSP
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const svgNS = "http://www.w3.org/2000/svg";
function s(tag, attrs) {
  const el = document.createElementNS(svgNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
  return el;
}
function money(x, cents) {
  const n = Number(x || 0);
  const whole = Math.abs(n - Math.round(n)) < 0.005;
  return (n < 0 ? "-$" : "$") + Math.abs(n).toLocaleString("en-US", {
    minimumFractionDigits: cents && !whole ? 2 : 0, maximumFractionDigits: cents && !whole ? 2 : 0 });
}
function shortDate(iso) {
  if (!iso) return "";
  const [y, m, d] = iso.slice(0, 10).split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString("en-US", { month: "short", day: "numeric" });
}
function monthYear(iso) {
  const [y, m] = iso.slice(0, 7).split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString("en-US", { month: "long", year: "numeric" });
}
function store(key, val) {
  try {
    if (val === undefined) return JSON.parse(sessionStorage.getItem(key) || "null");
    sessionStorage.setItem(key, JSON.stringify(val));
  } catch (e) { return null; }
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", body: JSON.stringify(body),
    headers: { "Content-Type": "application/json", "X-Debt-Agent": "1" } };
  const r = await fetch(path, { credentials: "same-origin", ...opts });
  let data = {};
  try { data = await r.json(); } catch (e) { /* empty */ }
  if (r.status === 401 && data.error === "signed_out") { showSignIn(); throw new Error("signed_out"); }
  if (!r.ok) throw new Error(data.error || "Couldn't reach the server. Check your connection and try again.");
  return data;
}

function render(...nodes) {
  view.replaceChildren(...nodes.flat().filter((n) => n !== null && n !== undefined && n !== false));
  window.scrollTo(0, 0);
}
function failed(err) {
  if (err.message === "signed_out") return;
  render(h("section", {}, h("h2", {}, "Couldn't load this"), h("p", { class: "lede" }, err.message),
    h("div", { class: "btn-row" }, h("button", { class: "btn", onclick: route }, "Try again"))));
}

/* ---------- sign in ---------- */
function showSignIn() {
  tabs.hidden = true;
  const expired = location.hash === "#expired";
  const err = h("p", { class: "error", role: "alert" }, expired ? "That login link was already used or has expired. Send /app again for a new one." : "");
  const input = h("input", { type: "text", inputmode: "text", autocomplete: "one-time-code", maxlength: "9",
    placeholder: "ABCD-EFGH", "aria-label": "Login code", autocapitalize: "characters", spellcheck: "false" });
  const btn = h("button", { class: "btn primary", type: "submit" }, "Sign in");
  const form = h("form", { onsubmit: async (e) => {
    e.preventDefault();
    btn.disabled = true; err.textContent = "";
    try {
      await api("/api/login", { code: input.value });
      location.hash = "#home";
      start();
    } catch (x) { err.textContent = x.message; btn.disabled = false; }
  } }, input, btn);
  const mark = h("img", { class: "mark", src: "/app/icon.svg", alt: "" });
  render(h("div", { class: "signin" }, mark,
    h("h1", {}, "Sign in to your dashboard"),
    h("p", { class: "lede" }, "Send /app to your Debt Agent bot in Telegram, then enter the code it replies with."),
    form, err));
  input.focus();
}

/* ---------- shared pieces ---------- */
function planCard(plan, onChange) {
  const proposed = plan.status === "proposed";
  const head = plan.deposit ? `Paycheck landed: ${money(plan.deposit)}` : "Payment plan";
  const total = plan.payments.reduce((a, p) => a + p.amount, 0);
  const rows = plan.payments.map((p) => {
    const why = p.minimum && p.extra ? `${money(p.minimum, 1)} minimum + ${money(p.extra, 1)} extra`
      : p.minimum ? `Minimum, due ${shortDate(p.due_date)}`
      : (p.reason || "") === "your choice" ? "Extra payment you chose" : (p.reason || "").split("extra: ").pop();
    const actions = [];
    if (p.status !== "paid" && p.pay_url) actions.push(h("a", { class: "btn small", href: p.pay_url, target: "_blank", rel: "noopener noreferrer" }, "Pay"));
    if (plan.status === "approved" && p.status !== "paid") actions.push(h("button", { class: "btn small", onclick: async (e) => {
      e.target.disabled = true; onChange(await api(`/api/payment/${p.id}/paid`, {})); } }, "Mark paid"));
    if (p.status === "paid") actions.push(h("span", { class: "small good-text" }, "Paid"));
    return h("div", { class: "pay" + (p.status === "paid" ? " paid" : "") },
      h("div", { class: "who" }, h("strong", {}, p.name), h("small", {}, why)),
      h("div", { class: "btn-row", style: "margin:0;flex-wrap:nowrap;align-items:center" },
        h("span", { class: "display num", style: "font-size:20px;font-weight:600" }, money(p.amount, 1)), ...actions));
  });
  return h("div", { class: "plan" },
    h("h2", {}, head),
    h("p", { class: "small", style: "margin:0" }, plan.payments.length ? `${money(total, 1)} across ${plan.payments.length} payment${plan.payments.length > 1 ? "s" : ""}. Bills and your buffer are already set aside.` : "Nothing to pay from this one."),
    rows,
    (plan.notes || []).map((n) => h("p", { class: "small over-text" }, n)),
    proposed ? h("div", { class: "btn-row" },
      h("button", { class: "btn primary", onclick: async (e) => { e.target.disabled = true; onChange(await api(`/api/plan/${plan.id}/approve`, {})); } }, "Approve plan"),
      h("button", { class: "btn", onclick: async (e) => { e.target.disabled = true; onChange(await api(`/api/plan/${plan.id}/skip`, {})); } }, "Skip"))
      : h("p", { class: "small" }, "Approved. Pay each one, then tap Mark paid."));
}

function paceStrip(day, days, used, budget) {
  const usedPct = budget ? Math.min(used / budget, 1.2) : 0;
  const dayPct = days ? day / days : 0;
  const over = used > budget || usedPct > dayPct + 0.1;
  const fill = h("div", { class: "pace-fill" + (over ? " over" : ""), style: `width:${Math.min(usedPct, 1) * 100}%` });
  const today = h("div", { class: "pace-today", style: `left:calc(${dayPct * 100}% - 1px)` });
  return h("div", { class: "pace" },
    h("div", { class: "pace-track", role: "img",
      "aria-label": `Day ${day} of ${days}. ${Math.round(usedPct * 100)} percent of the budget used.` }, fill, today),
    h("div", { class: "pace-legend" },
      h("span", {}, `Day ${day} of ${days}`),
      h("span", { class: over ? "over-text" : "" }, `${Math.round(usedPct * 100)}% of budget used`)));
}

/* ---------- Home ---------- */
async function home() {
  const d = await api("/api/home");
  const b = d.budget;
  const snap = d.snapshot;
  const top = [];
  if (b.has_budget) {
    if (b.left >= 0) {
      top.push(h("h1", {}, h("span", { class: "amount num" }, money(b.left)), ` left to spend in ${b.month_name}`));
      top.push(h("p", { class: "lede" }, b.days_left ? `${b.days_left} day${b.days_left > 1 ? "s" : ""} to go, about ${money(b.per_day_left)} a day.` : "Last day of the month."));
    } else {
      top.push(h("h1", {}, `${b.month_name} is `, h("span", { class: "amount num over-text" }, money(-b.left)), " over budget"));
      top.push(h("p", { class: "lede" }, `${money(b.budgeted_spent)} spent of a ${money(b.budget_total)} budget.`));
    }
    top.push(paceStrip(b.day, b.days, b.budgeted_spent, b.budget_total));
  } else {
    top.push(h("h1", {}, h("span", { class: "amount num" }, money(b.spent)), ` spent so far in ${b.month_name}`));
    top.push(h("p", { class: "lede" }, "Set a budget to see what's left to spend each day."));
    top.push(h("div", { class: "btn-row" }, h("a", { class: "btn primary", href: "#budget" }, "Set up your budget")));
  }

  const money_rows = h("ul", { class: "rows" },
    h("li", {}, h("span", { class: "label" }, "Cash in your accounts", h("small", {}, snap.checking_account ? `${snap.checking_account} ${money(snap.checking_balance)}, buffer ${money(snap.buffer)}` : "No checking account linked")),
      h("span", { class: "value num" }, money(d.cash_total))),
    h("li", {}, h("span", { class: "label" }, "Owed on cards and loans"), h("span", { class: "value num" }, money(snap.total_debt))),
    h("li", {}, h("span", { class: "label" }, `Income in ${b.month_name}`), h("span", { class: "value num" }, money(b.income))),
    h("li", {}, h("span", { class: "label" }, `Card and loan payments in ${b.month_name}`), h("span", { class: "value num" }, money(b.debt_payments))),
    h("li", {}, h("span", { class: "label" }, "Next payday"), h("span", { class: "value" }, shortDate(snap.next_payday))));

  const up = [...d.upcoming.bills.map((x) => ({ ...x, kind: "Bill" })), ...d.upcoming.debt_payments.map((x) => ({ name: x.name, amount: x.minimum, due: x.due, kind: "Minimum payment" }))]
    .sort((a, z) => a.due.localeCompare(z.due));
  const upcoming = up.length ? h("ul", { class: "rows" }, up.map((x) =>
    h("li", {}, h("span", { class: "label" }, x.name, h("small", {}, `${x.kind}, ${shortDate(x.due)}`)), h("span", { class: "value num" }, money(x.amount, 1)))))
    : h("p", { class: "small" }, "Nothing due in the next two weeks.");

  const rerender = (fresh) => { if (fresh) home().catch(failed); };
  render(
    h("section", {}, top),
    d.problems.length ? h("section", {}, h("div", { class: "notice" },
      h("strong", {}, "A bank needs attention"),
      d.problems.map((p) => h("p", { class: "small", style: "margin:4px 0 0" }, `${p.name}: ${p.status === "login_required" ? "log in again with /link in Telegram" : (p.problem || "couldn't sync")}`)))) : null,
    d.plan ? h("section", {}, planCard(d.plan, rerender)) : null,
    h("section", {}, h("h2", {}, "Where things stand"), money_rows),
    h("section", {}, h("h2", {}, "Due in the next two weeks"), upcoming),
    h("div", { class: "foot" }, h("span", {}, `Updated ${new Date().toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" })}`),
      h("button", { class: "btn small", onclick: () => home().catch(failed) }, "Refresh")));
}

/* ---------- Budget ---------- */
async function budget(editing) {
  const b = await api("/api/budget");
  if (!b.has_budget) return budgetSetup(b);

  const inputs = {};
  const amountInput = (key, val) => (inputs[key] = h("input", { type: "number", inputmode: "decimal", min: "0", step: "10", value: val ?? "", "aria-label": `Budget for ${key}` }));
  const rows = b.categories.map((c) => {
    if (editing) {
      return h("div", { class: "cat" }, h("div", { class: "cat-head" },
        h("span", { class: "cat-name" }, c.category, h("div", { class: "small" }, `${money(c.spent)} spent this month`)),
        amountInput(c.key, c.limit)));
    }
    if (c.limit === null) return null;
    const pct = c.limit ? c.spent / c.limit : 0;
    const cls = c.status === "over" ? "over" : c.status === "ahead" ? "over" : "";
    const note = c.status === "over" ? h("span", { class: "over-text" }, `Over by ${money(-c.left, 1)}`)
      : c.status === "ahead" ? h("span", { class: "over-text" }, `${money(c.left, 1)} left, spending faster than the month`)
      : h("span", {}, `${money(c.left, 1)} left`);
    return h("div", { class: "cat" },
      h("div", { class: "cat-head" }, h("span", { class: "cat-name" }, c.category),
        h("span", { class: "cat-figs num" }, `${money(c.spent)} of ${money(c.limit)}`)),
      h("div", { class: "bar", role: "img", "aria-label": `${c.category}: ${Math.round(pct * 100)} percent used` },
        h("div", { class: "bar-fill " + cls, style: `width:${Math.min(pct, 1) * 100}%` }),
        h("div", { class: "bar-tick", style: `left:calc(${(b.day / b.days) * 100}% - 1px)` })),
      h("div", { class: "cat-note" }, note));
  });
  const outside = b.categories.filter((c) => c.limit === null && c.spent > 0);

  const save = async () => {
    const limits = {};
    for (const [k, el] of Object.entries(inputs)) limits[k] = el.value === "" ? null : Number(el.value);
    await api("/api/budget", { limits });
    budget(false).catch(failed);
  };
  render(
    h("section", {},
      h("h1", {}, `${b.month_name} budget`),
      h("p", { class: "lede num" }, `${money(b.budgeted_spent)} spent of ${money(b.budget_total)}. ${b.left >= 0 ? money(b.left) + " left" : money(-b.left) + " over"}.`),
      editing ? null : paceStrip(b.day, b.days, b.budgeted_spent, b.budget_total)),
    h("section", {},
      editing ? h("p", { class: "small" }, "Monthly limit for each category. Leave a box empty to take it out of the budget.") :
        h("p", { class: "small" }, "The dark tick on each bar is today. A bar past the tick is spending faster than the month."),
      rows),
    !editing && outside.length ? h("section", {}, h("h2", {}, "Not in your budget"),
      h("ul", { class: "rows" }, outside.map((c) => h("li", {}, h("span", { class: "label" }, c.category), h("span", { class: "value num" }, money(c.spent)))))) : null,
    h("div", { class: "btn-row" }, editing
      ? [h("button", { class: "btn primary", onclick: (e) => { e.target.disabled = true; save().catch(failed); } }, "Save budget"),
         h("button", { class: "btn", onclick: () => budget(false).catch(failed) }, "Cancel")]
      : h("button", { class: "btn", onclick: () => budget(true).catch(failed) }, "Edit budget")));
}

function budgetSetup(b) {
  const sg = b.suggested || { limits: {}, names: {}, total: 0 };
  const keys = Object.keys(sg.limits).sort((a, z) => sg.limits[z] - sg.limits[a]);
  const inputs = {};
  const total = h("span", { class: "num" }, money(sg.total));
  const recount = () => { total.textContent = money(Object.values(inputs).reduce((a, el) => a + (Number(el.value) || 0), 0)); };
  const rows = keys.map((k) => h("div", { class: "cat" }, h("div", { class: "cat-head" },
    h("span", { class: "cat-name" }, sg.names[k]),
    (inputs[k] = h("input", { type: "number", inputmode: "decimal", min: "0", step: "10", value: sg.limits[k], "aria-label": `Budget for ${sg.names[k]}`, oninput: recount })))));
  render(
    h("section", {}, h("h1", {}, "Set up your budget"),
      h("p", { class: "lede" }, keys.length
        ? "These are your average monthly amounts from the last three months. Lower the ones you want to cut back on, then save."
        : "There isn't enough history yet to suggest amounts. Check back after a few weeks of transactions, or ask in Chat to set one.")),
    keys.length ? h("section", {}, rows, h("p", {}, "Monthly total: ", total)) : null,
    keys.length ? h("div", { class: "btn-row" }, h("button", { class: "btn primary", onclick: async (e) => {
      e.target.disabled = true;
      const limits = {};
      for (const [k, el] of Object.entries(inputs)) limits[k] = el.value === "" ? null : Number(el.value);
      try { await api("/api/budget", { limits, replace: true }); budget(false).catch(failed); } catch (x) { failed(x); }
    } }, "Save this budget")) : null);
}

/* ---------- Spending ---------- */
async function spending() {
  const d = await api(`/api/spending?days=${state.spendDays}`);
  const sp = d.spending;
  const subs = d.subscriptions;
  const seg = h("div", { class: "seg", role: "group", "aria-label": "Time period" },
    [7, 30, 90].map((n) => h("button", { "aria-pressed": String(state.spendDays === n), onclick: () => { state.spendDays = n; spending().catch(failed); } }, `${n} days`)));
  const max = Math.max(1, ...sp.categories.map((c) => Math.max(c.spent, c.normal || 0)));
  const cats = sp.categories.map((c) => {
    const change = c.change_pct !== null && c.change_pct !== 0 && (c.normal || 0) >= 25
      ? h("span", { class: c.change_pct > 0 ? "over-text" : "" }, `${c.change_pct > 0 ? "+" : ""}${c.change_pct}% vs usual`) : null;
    return h("div", { class: "cat" },
      h("div", { class: "cat-head" }, h("span", { class: "cat-name" }, c.category), h("span", { class: "cat-figs num" }, money(c.spent))),
      h("div", { class: "bar", role: "img", "aria-label": `${c.category} ${money(c.spent)}${c.normal ? ", usual " + money(c.normal) : ""}` },
        h("div", { class: "bar-fill plain", style: `width:${(c.spent / max) * 100}%` }),
        c.normal ? h("div", { class: "bar-tick", style: `left:calc(${(c.normal / max) * 100}% - 1px)` }) : null),
      change ? h("div", { class: "cat-note" }, change) : null);
  });
  const vs = sp.normal_total ? `${money(Math.abs(sp.total - sp.normal_total))} ${sp.total > sp.normal_total ? "more" : "less"} than your usual ${money(sp.normal_total)}.` : "";

  const subRows = subs.subscriptions.map((r) => {
    const flagged = r.flagged;
    return h("li", {}, h("span", { class: "label" }, r.name,
      h("small", {}, `${money(r.amount, 1)} ${r.frequency === "annually" ? "a year" : r.frequency === "monthly" ? "a month" : r.frequency}${r.price_up ? `, up ${money(r.price_up, 1)}` : ""}${flagged ? ", flagged to cancel" : ""}`)),
      h("button", { class: "btn small", "aria-pressed": String(flagged), onclick: async (e) => {
        e.target.disabled = true;
        await api(`/api/subs/${r.id}`, { kind: flagged ? "subscription" : "cancel" });
        spending().catch(failed);
      } }, flagged ? "Keep it" : "Flag to cancel"));
  });

  const search = h("input", { type: "search", placeholder: "Search merchants or categories", value: state.txQuery, "aria-label": "Search transactions" });
  const txList = h("div", {});
  const loadTx = async () => {
    state.txQuery = search.value;
    const t = await api(`/api/transactions?days=${Math.max(state.spendDays, 30)}&q=${encodeURIComponent(search.value)}`);
    const shown = t.transactions.slice(0, state.txShown || 25);
    const more = t.transactions.length > shown.length
      ? h("div", { class: "btn-row" }, h("button", { class: "btn", onclick: () => { state.txShown = (state.txShown || 25) + 50; loadTx().catch(failed); } }, `Show more (${t.transactions.length - shown.length} left)`)) : null;
    txList.replaceChildren(...(shown.length ? [...shown.map((x) => h("div", { class: "tx" },
      h("span", { class: "m" }, x.merchant), h("span", { class: "a num" + (x.amount < 0 ? " good-text" : "") }, x.amount < 0 ? "+" + money(-x.amount, 1) : money(x.amount, 1)),
      h("span", { class: "d" }, `${shortDate(x.date)}, ${x.category}${x.account ? ", " + x.account : ""}`))), more].filter(Boolean) : [h("p", { class: "small" }, "No transactions match.")]));
  };
  let timer;
  search.addEventListener("input", () => { state.txShown = 25; clearTimeout(timer); timer = setTimeout(() => loadTx().catch(failed), 300); });

  render(
    h("section", {}, seg,
      h("h1", { style: "margin-top:16px" }, h("span", { class: "amount num" }, money(sp.total)), ` spent in the last ${sp.days} days`),
      vs ? h("p", { class: "lede" }, vs) : null),
    h("section", {}, h("h2", {}, "By category"),
      h("p", { class: "small" }, "The dark tick is what you usually spend in the same number of days."), cats),
    sp.repeat_small_purchases.length ? h("section", {}, h("h2", {}, "Small purchases that add up"),
      h("ul", { class: "rows" }, sp.repeat_small_purchases.map((x) => h("li", {},
        h("span", { class: "label" }, x.merchant, h("small", {}, `${x.count} times, about ${money(x.average, 1)} each`)),
        h("span", { class: "value num" }, money(x.total)))))) : null,
    h("section", {}, h("h2", {}, "Subscriptions"),
      h("p", { class: "lede num", style: "margin-top:0" }, `${money(subs.monthly_total, 1)} a month, ${money(subs.yearly_total)} a year.`),
      Object.entries(subs.overlaps).map(([k, names]) => h("p", { class: "small" }, `${names.length} ${k.toLowerCase()} services: ${names.join(", ")}.`)),
      subRows.length ? h("ul", { class: "rows" }, subRows) : h("p", { class: "small" }, "None found yet.")),
    h("section", {}, h("h2", {}, "Transactions"), search, txList));
  loadTx().catch(failed);
}

/* ---------- Debt ---------- */
async function debt() {
  const d = await api(`/api/debt?extra=${state.extra}`);
  const p = d.projection;
  const head = h("h1", {});
  const lede = h("p", { class: "lede" });
  const chartBox = h("div", { class: "chart" });
  const legend = h("div", { class: "legend" });
  const label = h("label", { for: "extra" });
  const slider = h("input", { id: "extra", type: "range", min: "0", max: "3000", step: "50", value: String(state.extra) });

  const paint = (proj) => {
    const w = proj.with_extra, m = proj.minimums_only;
    head.replaceChildren(w.debt_free ? `Debt-free by ${monthYear(w.debt_free)}` : "Not paid off at this rate");
    const saved = m.total_interest - w.total_interest;
    lede.textContent = d.total_debt <= 0 ? "No debt on your linked accounts." :
      w.debt_free && m.debt_free ? `Paying ${money(proj.monthly_minimums + proj.extra)} a month instead of just the ${money(proj.monthly_minimums)} in minimums saves ${money(saved)} in interest and finishes ${Math.max(m.months - w.months, 0)} months sooner.`
      : w.debt_free ? `Minimum payments alone wouldn't pay this off. With ${money(proj.extra)} extra a month, you'd pay ${money(w.total_interest)} in interest along the way.`
      : "At this amount the interest outpaces the payments. Try a higher extra amount.";
    label.textContent = `Extra toward debt each month: ${money(proj.extra)}`;
    legend.replaceChildren(h("span", {}, `With ${money(proj.extra)} extra`), h("span", { class: "min" }, "Minimums only"));
    chartBox.replaceChildren(lineChart([
      { cls: "plan", name: `With ${money(proj.extra)} extra`, data: w.balances },
      { cls: "min", name: "Minimums only", data: m.balances.slice(0, Math.max(w.balances.length * 2, 24)) },
    ]));
  };
  paint(p);
  let timer;
  slider.addEventListener("input", () => {
    state.extra = Number(slider.value);
    label.textContent = `Extra toward debt each month: ${money(state.extra)}`;
    clearTimeout(timer);
    timer = setTimeout(async () => { try { paint((await api(`/api/debt?extra=${state.extra}`)).projection); } catch (x) { failed(x); } }, 250);
  });

  const order = p.with_extra.payoff_order;
  const debts = [...d.debts].sort((a, z) => (order.indexOf(a.name) + 99) % 99 - (order.indexOf(z.name) + 99) % 99);
  const rows = debts.map((x) => {
    const unknownRate = x.apr === 0 && !x.promo_end;
    const rate = x.promo_end && x.apr_now === 0 ? `0% until ${shortDate(x.promo_end)}, then ${x.apr.toFixed(2)}%` : unknownRate ? "rate not shared by the bank" : `${x.apr.toFixed(2)}% APR`;
    const when = p.with_extra.payoff_month[x.name];
    return h("li", {}, h("span", { class: "label" }, x.name,
      h("small", {}, `${rate}${x.min_payment ? `, ${money(x.min_payment, 1)} minimum` : ""}${when ? `, paid off in ${when} month${when > 1 ? "s" : ""}` : ""}`)),
      h("span", { class: "value num" }, money(x.balance)));
  });
  const strat = h("div", { class: "seg", role: "group", "aria-label": "Payoff order" },
    ["avalanche", "snowball"].map((k) => h("button", { "aria-pressed": String(p.strategy === k), onclick: async () => {
      await api("/api/settings", { strategy: k }); debt().catch(failed); } }, k === "avalanche" ? "Highest rate first" : "Smallest balance first")));

  render(
    h("section", {}, head, lede),
    h("section", {}, label, slider, legend, chartBox),
    h("section", {}, h("h2", {}, "Payoff order"), strat, h("ul", { class: "rows", style: "margin-top:12px" }, rows),
      h("p", { class: "small" }, "If a rate shows as not shared, tell me the real rate in Chat or Telegram so the plan counts its interest.")));
}

function lineChart(series) {
  const W = 340, H = 200, L = 44, R = 12, T = 12, B = 26;
  const n = Math.max(...series.map((x) => x.data.length));
  const maxY = Math.max(1, ...series.flatMap((x) => x.data));
  const X = (i) => L + (i / Math.max(n - 1, 1)) * (W - L - R);
  const Y = (v) => T + (1 - v / maxY) * (H - T - B);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": series.map((x) => `${x.name}: ${x.data.length - 1} months`).join("; ") });
  const step = niceStep(maxY);
  for (let v = 0; v <= maxY + 1; v += step) {
    svg.append(s("line", { class: "grid", x1: L, x2: W - R, y1: Y(v), y2: Y(v) }));
    const t = s("text", { class: "axis-text", x: L - 6, y: Y(v) + 4, "text-anchor": "end" });
    t.textContent = v >= 1000 ? `$${Math.round(v / 1000)}k` : `$${v}`;
    svg.append(t);
  }
  const yrs = Math.ceil((n - 1) / 12);
  for (let yr = 0; yr <= yrs; yr += yrs > 6 ? 2 : 1) {
    const i = yr * 12;
    if (i > n - 1) break;
    const t = s("text", { class: "axis-text", x: X(i), y: H - 6, "text-anchor": i === 0 ? "start" : "middle" });
    t.textContent = yr === 0 ? "Now" : `${yr} yr`;
    svg.append(t);
  }
  for (const sr of series) {
    const d = sr.data.map((v, i) => `${i ? "L" : "M"}${X(i).toFixed(1)},${Y(v).toFixed(1)}`).join("");
    svg.append(s("path", { class: `line ${sr.cls}`, d }));
  }
  // Hover / touch: crosshair with both balances
  const box = h("div", {});
  const tip = h("div", { class: "tip", hidden: true });
  const cross = s("line", { class: "cross", y1: T, y2: H - B, visibility: "hidden" });
  const dots = series.map((sr) => { const c = s("circle", { r: 4, class: "dot", fill: sr.cls === "plan" ? "var(--good)" : "var(--over)", visibility: "hidden" }); svg.append(c); return c; });
  svg.append(cross);
  const hit = s("rect", { x: L, y: T, width: W - L - R, height: H - T - B, fill: "transparent" });
  svg.append(hit);
  const move = (clientX) => {
    const r = svg.getBoundingClientRect();
    const px = ((clientX - r.left) / r.width) * W;
    const i = Math.max(0, Math.min(n - 1, Math.round(((px - L) / (W - L - R)) * (n - 1))));
    cross.setAttribute("x1", X(i)); cross.setAttribute("x2", X(i)); cross.setAttribute("visibility", "visible");
    const lines = [h("strong", {}, i === 0 ? "Now" : `In ${i} month${i > 1 ? "s" : ""}`)];
    series.forEach((sr, k) => {
      const v = sr.data[Math.min(i, sr.data.length - 1)];
      dots[k].setAttribute("cx", X(i)); dots[k].setAttribute("cy", Y(v)); dots[k].setAttribute("visibility", "visible");
      lines.push(h("div", {}, `${sr.name}: ${money(v)}`));
    });
    tip.replaceChildren(...lines);
    tip.hidden = false;
    const left = (X(i) / W) * r.width;
    tip.style.left = `${Math.min(Math.max(left - 80, 0), r.width - 170)}px`;
    tip.style.top = "0px";
  };
  const hide = () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); dots.forEach((c) => c.setAttribute("visibility", "hidden")); };
  hit.addEventListener("pointermove", (e) => move(e.clientX));
  hit.addEventListener("pointerdown", (e) => move(e.clientX));
  hit.addEventListener("pointerleave", hide);
  box.append(svg, tip);
  return box;
}
function niceStep(max) {
  const raw = max / 4;
  const p = Math.pow(10, Math.floor(Math.log10(raw)));
  const f = raw / p;
  return (f <= 1 ? 1 : f <= 2 ? 2 : f <= 5 ? 5 : 10) * p;
}

/* ---------- Chat ---------- */
const SUGGESTIONS = [
  "What should I do with my next paycheck?",
  "Where is my money going this month?",
  "Which subscriptions should I cut?",
  "How fast can I pay off my cards with $500 extra a month?",
];
function chat() {
  const history = store("chat") || [];
  const log = h("div", { class: "chat-log" });
  const input = h("input", { type: "text", placeholder: "Ask about your money", "aria-label": "Message", enterkeyhint: "send" });
  const send = h("button", { class: "btn primary", type: "submit" }, "Send");

  const bubble = (role, text) => h("div", { class: `msg ${role === "user" ? "user" : "bot"}` }, text);
  const actionNode = (a) => {
    if (a.type === "plan" && a.plan) return planCard(a.plan, () => { location.hash = "#home"; });
    if (a.type === "confirm_settings") {
      const desc = Object.entries(a.changes).map(([k, v]) => `${k} to ${k === "buffer" ? money(v) : v}`).join(", ");
      const wrap = h("div", { class: "msg bot" }, `Change ${desc}?`);
      wrap.append(h("div", { class: "btn-row" },
        h("button", { class: "btn small primary", onclick: async () => { await api("/api/settings", a.changes); wrap.replaceChildren(`Changed ${desc}.`); } }, "Yes, change it"),
        h("button", { class: "btn small", onclick: () => wrap.replaceChildren("Okay, nothing changed.") }, "No")));
      return wrap;
    }
    return null;
  };
  for (const m of history) log.append(bubble(m.role, m.content));

  const ask = async (text) => {
    if (!text.trim()) return;
    chips.hidden = true;
    log.append(bubble("user", text));
    const pending = h("div", { class: "msg bot pending" }, "Looking at your numbers…");
    log.append(pending);
    pending.scrollIntoView({ block: "end" });
    input.value = ""; send.disabled = true;
    try {
      const r = await api("/api/chat", { text, history: history.slice(-12) });
      pending.replaceWith(bubble("assistant", r.reply));
      for (const a of r.actions) { const n = actionNode(a); if (n) log.append(n); }
      history.push({ role: "user", content: text }, { role: "assistant", content: r.reply });
      store("chat", history.slice(-24));
    } catch (x) {
      pending.replaceWith(h("div", { class: "msg bot" }, x.message === "signed_out" ? "" : x.message));
    }
    send.disabled = false;
    log.lastElementChild && log.lastElementChild.scrollIntoView({ block: "end" });
  };
  const chips = h("div", { class: "chips", hidden: history.length > 0 }, SUGGESTIONS.map((q) => h("button", { onclick: () => ask(q) }, q)));
  const form = h("form", { onsubmit: (e) => { e.preventDefault(); ask(input.value); } }, input, send);

  render(
    h("section", {}, h("h1", {}, "Ask about your money"),
      h("p", { class: "lede" }, state.chatOn ? "Answers use your real accounts, budget and payoff math." : "Chat isn't turned on: add an Anthropic API key in your Render settings."),
      history.length ? h("div", { class: "btn-row" }, h("button", { class: "btn small", onclick: () => { store("chat", []); chat(); } }, "Start over")) : null),
    h("section", {}, log, chips),
    h("div", { class: "chat-pad" }),
    h("div", { class: "composer" }, form));
  if (log.lastElementChild) log.lastElementChild.scrollIntoView({ block: "end" });
}

/* ---------- routing ---------- */
const ROUTES = { home, budget: () => budget(false), spending, debt, chat };
function route() {
  const name = (location.hash || "#home").slice(1);
  const fn = ROUTES[name] || home;
  const current = `#${ROUTES[name] ? name : "home"}`;
  for (const a of tabs.querySelectorAll("a")) {
    if (a.getAttribute("href") === current) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  }
  render(h("p", { class: "loading" }, "Loading…"));
  Promise.resolve().then(fn).catch(failed);
}

async function start() {
  try {
    const me = await api("/api/me");
    state.chatOn = me.chat;
  } catch (e) { return; }
  tabs.hidden = false;
  if (!location.hash || location.hash === "#expired") history.replaceState(null, "", "#home");
  route();
}
window.addEventListener("hashchange", () => { if (!tabs.hidden) route(); });
start();
