"""Read-only Plaid sync: balances, card/loan details, and new transactions.

Only these Plaid products are used: Transactions and Liabilities (balances
come with both). No transfer product is ever enabled, so nothing here can
move money.
"""
from __future__ import annotations

import logging
from datetime import date

import plaid
from plaid.api import plaid_api
from plaid.model.accounts_get_request import AccountsGetRequest
from plaid.model.country_code import CountryCode
from plaid.model.item_public_token_exchange_request import ItemPublicTokenExchangeRequest
from plaid.model.link_token_create_request import LinkTokenCreateRequest
from plaid.model.link_token_create_request_user import LinkTokenCreateRequestUser
from plaid.model.link_token_transactions import LinkTokenTransactions
from plaid.model.liabilities_get_request import LiabilitiesGetRequest
from plaid.model.products import Products
from plaid.model.sandbox_public_token_create_request import SandboxPublicTokenCreateRequest
from plaid.model.sandbox_public_token_create_request_options import SandboxPublicTokenCreateRequestOptions
from plaid.model.transactions_recurring_get_request import TransactionsRecurringGetRequest
from plaid.model.transactions_sync_request import TransactionsSyncRequest

from .store import Store

log = logging.getLogger(__name__)

HOSTS = {"sandbox": plaid.Environment.Sandbox, "production": plaid.Environment.Production}


def client(client_id: str, secret: str, env: str = "sandbox") -> plaid_api.PlaidApi:
    conf = plaid.Configuration(host=HOSTS[env], api_key={"clientId": client_id, "secret": secret})
    return plaid_api.PlaidApi(plaid.ApiClient(conf))


def _s(v) -> str | None:
    if v is None:
        return None
    return v.isoformat() if isinstance(v, date) else str(v)


def link_sandbox_bank(api: plaid_api.PlaidApi, store: Store, institution_id: str = "ins_109508") -> str:
    """Sandbox only: create a fake bank login (First Platypus Bank) without the Link popup."""
    pub = api.sandbox_public_token_create(SandboxPublicTokenCreateRequest(
        institution_id=institution_id,
        initial_products=[Products("transactions"), Products("liabilities")],
        # Plaid's test user with realistic, recurring transaction history
        options=SandboxPublicTokenCreateRequestOptions(override_username="user_transactions_dynamic",
                                                       override_password="pass_good"),
    ))
    ex = api.item_public_token_exchange(ItemPublicTokenExchangeRequest(public_token=pub.public_token))
    store.add_institution(ex.item_id, "Sandbox: First Platypus Bank", ex.access_token)
    return ex.item_id


def create_link_token(api: plaid_api.PlaidApi, user_id: str = "owner", redirect_uri: str | None = None) -> str:
    """A token the Link page uses to open Plaid's bank login window."""
    kwargs = dict(
        user=LinkTokenCreateRequestUser(client_user_id=user_id),
        client_name="Debt Agent",
        products=[Products("transactions"), Products("liabilities")],
        country_codes=[CountryCode("US")],
        language="en",
        transactions=LinkTokenTransactions(days_requested=365),  # a year of history for spending trends
    )
    if redirect_uri:  # needed for OAuth banks (Chase, Capital One...) on a phone
        kwargs["redirect_uri"] = redirect_uri
    return api.link_token_create(LinkTokenCreateRequest(**kwargs)).link_token


def exchange_public_token(api: plaid_api.PlaidApi, store: Store, public_token: str, name: str) -> str:
    ex = api.item_public_token_exchange(ItemPublicTokenExchangeRequest(public_token=public_token))
    store.add_institution(ex.item_id, name, ex.access_token)
    return ex.item_id


def sync_all(api: plaid_api.PlaidApi, store: Store) -> list[str]:
    """Pull every linked bank. Returns names of banks that need you to log in again."""
    broken = []
    for inst in store.institutions():
        try:
            _sync_item(api, store, inst)
            store.set_institution(inst["item_id"], status="ok")
        except plaid.ApiException as e:
            body = getattr(e, "body", "") or ""
            log.warning("Plaid sync failed for %s: %s", inst["name"], body[:300])
            if "ITEM_LOGIN_REQUIRED" in body:
                store.set_institution(inst["item_id"], status="login_required")
                broken.append(inst["name"])
            else:
                store.set_institution(inst["item_id"], status="error")
    return broken


def _sync_item(api: plaid_api.PlaidApi, store: Store, inst: dict):
    token = inst["access_token"]

    # 1. Accounts and balances
    accts = api.accounts_get(AccountsGetRequest(access_token=token)).to_dict()["accounts"]
    for a in accts:
        bal = a["balances"]
        store.upsert_account({
            "id": a["account_id"], "item_id": inst["item_id"], "name": a["name"],
            "official_name": a.get("official_name"), "mask": a.get("mask"),
            "type": str(a["type"]), "subtype": str(a.get("subtype") or ""),
            "balance": float(bal.get("current") or 0), "source": "plaid", "updated_at": None,
        })

    # 2. Card and loan details (rates, minimums, due dates)
    try:
        liab = api.liabilities_get(LiabilitiesGetRequest(access_token=token)).to_dict()["liabilities"]
    except plaid.ApiException as e:
        log.info("No liabilities for %s (%s)", inst["name"], (getattr(e, "body", "") or "")[:120])
        liab = {}
    for c in liab.get("credit") or []:
        aprs = c.get("aprs") or []
        purchase = next((x for x in aprs if str(x.get("apr_type")) == "purchase_apr"), aprs[0] if aprs else None)
        store.upsert_account({
            "id": c["account_id"],
            "apr": float(purchase["apr_percentage"]) if purchase else None,
            "min_payment": c.get("minimum_payment_amount"),
            "due_date": _s(c.get("next_payment_due_date")),
        })
    for s in liab.get("student") or []:
        store.upsert_account({
            "id": s["account_id"], "apr": s.get("interest_rate_percentage"),
            "min_payment": s.get("minimum_payment_amount"),
            "due_date": _s(s.get("next_payment_due_date")),
        })
    for m in liab.get("mortgage") or []:
        rate = (m.get("interest_rate") or {}).get("percentage")
        store.upsert_account({
            "id": m["account_id"], "apr": rate, "min_payment": m.get("next_monthly_payment"),
            "due_date": _s(m.get("next_payment_due_date")),
        })

    # 3. New transactions since last sync
    cursor = inst.get("cursor")
    first_sync = cursor is None
    while True:
        req = TransactionsSyncRequest(access_token=token, cursor=cursor) if cursor \
            else TransactionsSyncRequest(access_token=token)
        resp = api.transactions_sync(req).to_dict()
        for t in resp["added"] + (resp.get("modified") or []):
            # Plaid: positive amount = money out, negative = money in
            pfc = t.get("personal_finance_category") or {}
            store.add_transaction(t["transaction_id"], t["account_id"], _s(t["date"]),
                                  t.get("name") or "", -float(t["amount"]),
                                  merchant=t.get("merchant_name"), category=pfc.get("primary"),
                                  detailed=pfc.get("detailed"), pending=bool(t.get("pending")))
        for t in resp.get("removed") or []:
            store.remove_transaction(t["transaction_id"])
        cursor = resp["next_cursor"]
        if not resp["has_more"]:
            break
    store.set_institution(inst["item_id"], cursor=cursor)
    _sync_recurring(api, store, inst)
    if first_sync:
        # Don't fire payday alerts for months of old history.
        store.mark_all_processed()


def _sync_recurring(api: plaid_api.PlaidApi, store: Store, inst: dict):
    """Recurring charges and deposits Plaid has spotted (bills, subscriptions, pay)."""
    try:
        resp = api.transactions_recurring_get(
            TransactionsRecurringGetRequest(access_token=inst["access_token"])).to_dict()
    except plaid.ApiException as e:
        # Normal right after linking (PRODUCT_NOT_READY); it fills in on a later sync.
        log.info("Recurring not available yet for %s: %s", inst["name"], (getattr(e, "body", "") or "")[:160])
        return
    for direction, key in (("in", "inflow_streams"), ("out", "outflow_streams")):
        for s in resp.get(key) or []:
            pfc = s.get("personal_finance_category") or {}
            store.upsert_stream({
                "stream_id": s["stream_id"], "item_id": inst["item_id"], "account_id": s["account_id"],
                "direction": direction, "name": s.get("description") or "",
                "merchant": s.get("merchant_name"),
                "frequency": str(s.get("frequency") or "UNKNOWN").lower(),
                "avg_amount": abs(float((s.get("average_amount") or {}).get("amount") or 0)),
                "last_amount": abs(float((s.get("last_amount") or {}).get("amount") or 0)),
                "last_date": _s(s.get("last_date")), "next_date": _s(s.get("predicted_next_date")),
                "category": pfc.get("primary"), "detailed": pfc.get("detailed"),
                "status": str(s.get("status") or ""), "active": int(bool(s.get("is_active"))),
            })
