"""Dashboard over real HTTP: sign-in, sessions, request checks, budget."""
import http.client
import json
import socket

import pytest
from cryptography.fernet import Fernet

from debtbot.config import Config
from debtbot.dashboard import MAX_FAILS, Dashboard, _hash
from debtbot.service import Service
from debtbot.store import Store
from debtbot.web import LinkServer


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture()
def app(tmp_path, monkeypatch):
    port = free_port()
    monkeypatch.setenv("DEBTBOT_DB", str(tmp_path / "d.db"))
    monkeypatch.setenv("DEBTBOT_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("PUBLIC_URL", f"http://127.0.0.1:{port}")
    cfg = Config()
    svc = Service(cfg, Store(cfg.db_path, cfg.db_key))
    svc.refresh()
    dash = Dashboard(svc)
    server = LinkServer(svc, port, dashboard=dash).start()
    yield svc, dash, port
    server.shutdown()


def call(port, method, path, body=None, cookie=None, headers=None):
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    hdrs = {"Content-Type": "application/json", "X-Debt-Agent": "1", **(headers or {})}
    if cookie:
        hdrs["Cookie"] = cookie
    c.request(method, path, body=json.dumps(body) if body is not None else None, headers=hdrs)
    r = c.getresponse()
    raw = r.read()
    try:
        data = json.loads(raw)
    except ValueError:
        data = raw
    return r.status, data, r


def sign_in(dash, port):
    code, _ = dash.new_login()
    status, data, r = call(port, "POST", "/api/login", {"code": code})
    assert status == 200
    return r.getheader("Set-Cookie").split(";")[0]


def test_everything_needs_sign_in(app):
    _, _, port = app
    for path in ("/api/home", "/api/budget", "/api/spending", "/api/debt", "/api/transactions"):
        assert call(port, "GET", path)[0] == 401
    assert call(port, "GET", "/app")[0] == 200          # the empty shell is public, no data in it


def test_code_works_once_and_cookie_is_safe(app):
    svc, dash, port = app
    code, _ = dash.new_login()
    status, _, r = call(port, "POST", "/api/login", {"code": code.lower()})
    assert status == 200
    cookie = r.getheader("Set-Cookie")
    assert "HttpOnly" in cookie and "SameSite=Lax" in cookie
    sid = cookie.split(";")[0].split("=", 1)[1]
    # only a hash is stored
    rows = svc.store._q("SELECT id_hash FROM sessions")
    assert [x["id_hash"] for x in rows] == [_hash(sid)]
    assert call(port, "POST", "/api/login", {"code": code})[0] == 401   # reused
    assert call(port, "GET", "/api/home", cookie=cookie.split(";")[0])[0] == 200


def test_login_link(app):
    _, dash, port = app
    _, link = dash.new_login()
    status, _, r = call(port, "GET", f"/app/login?t={link}")
    assert status == 302 and r.getheader("Location") == "/app" and "dbs=" in r.getheader("Set-Cookie")
    status, _, r = call(port, "GET", f"/app/login?t={link}")
    assert r.getheader("Location") == "/app#expired"


def test_lockout_after_wrong_codes(app):
    _, dash, port = app
    for _ in range(MAX_FAILS):
        assert call(port, "POST", "/api/login", {"code": "AAAA-AAAA"})[0] == 401
    code, _ = dash.new_login()
    assert call(port, "POST", "/api/login", {"code": code})[0] == 429


def test_cross_site_requests_blocked(app):
    _, dash, port = app
    cookie = sign_in(dash, port)
    assert call(port, "POST", "/api/budget", {"limits": {"FOOD_AND_DRINK": 1}}, cookie,
                headers={"Origin": "https://evil.example"})[0] == 403
    c = http.client.HTTPConnection("127.0.0.1", port)
    c.request("POST", "/api/budget", body="{}", headers={"Cookie": cookie, "Content-Type": "application/json"})
    assert c.getresponse().status == 403                 # no app header


def test_budget_and_views(app):
    svc, dash, port = app
    cookie = sign_in(dash, port)
    st, b, _ = call(port, "GET", "/api/budget", cookie=cookie)
    assert st == 200 and not b["has_budget"] and b["suggested"]["limits"]
    st, b, _ = call(port, "POST", "/api/budget", {"limits": b["suggested"]["limits"], "replace": True}, cookie)
    assert b["has_budget"] and b["budget_total"] > 0
    st, home, _ = call(port, "GET", "/api/home", cookie=cookie)
    assert home["budget"]["has_budget"] and "snapshot" in home
    st, d, _ = call(port, "GET", "/api/debt?extra=300", cookie=cookie)
    assert d["projection"]["with_extra"]["balances"][0] == d["total_debt"]
    st, sp, _ = call(port, "GET", "/api/spending?days=30", cookie=cookie)
    assert sp["spending"]["total"] > 0 and sp["subscriptions"]["subscriptions"]


def test_logout_all(app):
    svc, dash, port = app
    cookie = sign_in(dash, port)
    svc.store.delete_all_sessions()
    assert call(port, "GET", "/api/home", cookie=cookie)[0] == 401


def test_static_has_strict_csp(app):
    _, _, port = app
    _, _, r = call(port, "GET", "/app/app.js")
    assert "script-src 'self'" in r.getheader("Content-Security-Policy")
    assert call(port, "GET", "/app/fonts/../../config.py")[0] == 404
