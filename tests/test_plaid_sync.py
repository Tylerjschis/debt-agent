"""Plaid sync against a fake Plaid API: several banks, missing details, errors."""
import json
import types
from datetime import date, timedelta

import plaid
from cryptography.fernet import Fernet

from debtbot import plaid_sync
from debtbot.messages import accounts_text
from debtbot.store import Store

TODAY = date.today()


def resp(d):
    return types.SimpleNamespace(to_dict=lambda: d)


def api_error(code, msg="nope"):
    e = plaid.ApiException(status=400, reason="Bad Request")
    e.body = json.dumps({"error_code": code, "error_message": msg})
    return e


BANKS = {
    # A credit union with checking and savings only; no card/loan details
    "tok-cu": {
        "accounts": [
            {"account_id": "cu-old", "name": "Old Checking", "mask": "0001", "type": "depository",
             "subtype": "checking", "balances": {"current": 0, "available": 0}},
            {"account_id": "cu-chk", "name": "Share Draft", "mask": "4321", "type": "depository",
             "subtype": "checking", "balances": {"current": None, "available": 2843.17}},
            {"account_id": "cu-sav", "name": "Savings", "mask": "9876", "type": "depository",
             "subtype": "savings", "balances": {"current": 5200, "available": 5200}},
        ],
        "liabilities": api_error("PRODUCTS_NOT_SUPPORTED"),
        "transactions": [
            {"transaction_id": "t1", "account_id": "cu-chk", "date": TODAY - timedelta(days=4),
             "name": "SHUMAKER ROOFING PAYROLL", "merchant_name": None, "amount": -3100, "pending": False,
             "personal_finance_category": {"primary": "INCOME", "detailed": "INCOME_WAGES"}},
        ],
    },
    # A card issuer with a card and a personal loan Plaid has no details for
    "tok-card": {
        "accounts": [
            {"account_id": "c-visa", "name": "Visa Signature GO REWARDS", "mask": "1111", "type": "credit",
             "subtype": "credit card", "balances": {"current": 5269.87}},
            {"account_id": "c-loan", "name": "Consolidation no collateral", "mask": "2222", "type": "loan",
             "subtype": "loan", "balances": {"current": 25774.90}},
        ],
        "liabilities": {"credit": [{"account_id": "c-visa", "aprs": [{"apr_type": "purchase_apr", "apr_percentage": 14.49}],
                                    "minimum_payment_amount": 252.53, "next_payment_due_date": TODAY + timedelta(days=26)}]},
        "transactions": [],
    },
    # A bank that needs a fresh login
    "tok-broken": {"accounts": api_error("ITEM_LOGIN_REQUIRED")},
}


class FakePlaid:
    def _bank(self, req):
        return BANKS[req["access_token"]]

    def accounts_get(self, req):
        a = self._bank(req)["accounts"]
        if isinstance(a, Exception):
            raise a
        return resp({"accounts": a})

    def liabilities_get(self, req):
        lib = self._bank(req)["liabilities"]
        if isinstance(lib, Exception):
            raise lib
        return resp({"liabilities": lib})

    def transactions_sync(self, req):
        return resp({"added": self._bank(req)["transactions"], "modified": [], "removed": [],
                     "next_cursor": "c1", "has_more": False})

    def transactions_recurring_get(self, req):
        raise api_error("PRODUCT_NOT_READY")


def test_many_banks(tmp_path):
    store = Store(tmp_path / "t.db", Fernet.generate_key().decode())
    store.add_institution("i-cu", "Frederick County Credit Union", "tok-cu")
    store.add_institution("i-card", "Card Bank", "tok-card")
    store.add_institution("i-broken", "Broken Bank", "tok-broken")

    broken = plaid_sync.sync_all(FakePlaid(), store)
    assert broken == ["Broken Bank"]

    # The checking account that receives pay is chosen, not the empty old one
    chk = store.checking()
    assert chk["id"] == "cu-chk" and chk["balance"] == 2843.17
    assert {a["name"] for a in store.cash_accounts()} == {"Old Checking", "Share Draft", "Savings"}
    assert store.checking("0001")["id"] == "cu-old"          # config can still pick by last 4

    debts = {d.name: d for d in store.debts()}
    assert debts["Visa Signature GO REWARDS"].apr == 14.49
    assert debts["Consolidation no collateral"].apr == 0       # no details from the bank

    banks = {b["name"]: b for b in store.institution_list()}
    assert "access_token" not in banks["Card Bank"] and "token_enc" not in banks["Card Bank"]
    assert banks["Broken Bank"]["status"] == "login_required"
    assert banks["Frederick County Credit Union"]["status"] == "ok"

    text = accounts_text(store.institution_list(), store.accounts(), chk["id"])
    assert "Share Draft ••4321 (checking): $2,843.17 ← checking used for plans" in text
    assert "Broken Bank" in text and "No accounts came through" in text
    assert "ITEM_LOGIN_REQUIRED" in text


def test_link_token_does_not_require_liabilities():
    seen = {}

    class Api:
        def link_token_create(self, req):
            seen.update(req.to_dict())
            return types.SimpleNamespace(link_token="link-x")

    plaid_sync.create_link_token(Api(), redirect_uri="https://x/oauth")
    assert seen["products"] == ["transactions"]
    assert seen["required_if_supported_products"] == ["liabilities"]
