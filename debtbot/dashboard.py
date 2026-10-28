"""The phone dashboard: a small web app served next to the bank-linking page.

Security model
- Nobody can open it without a login code from your Telegram (/app). Codes and
  login links are single use and expire after 10 minutes.
- Signing in creates a session cookie (HttpOnly, Secure, SameSite=Lax) that lasts
  30 days. Only a SHA-256 hash of the session id is stored, so a copy of the
  database can't be used to log in. /logout_all in Telegram ends every session.
- Changes (POST) must come from the app itself: a custom header plus an Origin
  check, which a page on another site can't fake.
- Repeated wrong codes lock code entry for 15 minutes.
- The app loads nothing from other sites (strict Content-Security-Policy).
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import secrets
import threading
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .service import Service

log = logging.getLogger(__name__)

STATIC = Path(__file__).resolve().parent / "static"
SESSION_DAYS = 30
CODE_TTL = 10 * 60
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
MAX_FAILS = 10
LOCKOUT = 15 * 60
COOKIE = "dbs"

CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
       "connect-src 'self'; font-src 'self'; manifest-src 'self'; frame-ancestors 'none'; "
       "base-uri 'none'; form-action 'self'")

ASSETS = {
    "/app": ("app.html", "text/html; charset=utf-8"),
    "/app/": ("app.html", "text/html; charset=utf-8"),
    "/app/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
    "/app/icon.svg": ("icon.svg", "image/svg+xml"),
    "/app/icon-192.png": ("icon-192.png", "image/png"),
    "/app/icon-512.png": ("icon-512.png", "image/png"),
}


def _hash(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


class Dashboard:
    def __init__(self, svc: Service, assistant=None):
        self.svc = svc
        self.assistant = assistant
        self._codes: dict[str, float] = {}   # login code -> expiry
        self._links: dict[str, float] = {}   # login link token -> expiry
        self._fails: list[float] = []
        self._lock = threading.Lock()

    # -- login codes (created by /app in Telegram) ----------------------------------
    def new_login(self) -> tuple[str, str]:
        """Returns (code to type, token for a one-tap login link)."""
        code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(8))
        link = secrets.token_urlsafe(24)
        with self._lock:
            now = time.time()
            self._codes = {k: v for k, v in self._codes.items() if v > now}
            self._links = {k: v for k, v in self._links.items() if v > now}
            self._codes[code] = now + CODE_TTL
            self._links[link] = now + CODE_TTL
        return f"{code[:4]}-{code[4:]}", link

    def _use(self, table: dict, key: str) -> bool:
        with self._lock:
            exp = table.pop(key, 0)
            return exp > time.time()

    def _locked(self) -> bool:
        with self._lock:
            now = time.time()
            self._fails = [f for f in self._fails if f > now - LOCKOUT]
            return len(self._fails) >= MAX_FAILS

    def _failed(self):
        with self._lock:
            self._fails.append(time.time())

    def _new_session(self, agent: str) -> str:
        sid = secrets.token_urlsafe(32)
        self.svc.store.add_session(_hash(sid), time.time() + SESSION_DAYS * 86400, agent)
        return sid

    def _session(self, h) -> str | None:
        """The caller's session hash if signed in."""
        c = SimpleCookie(h.headers.get("Cookie") or "")
        sid = c[COOKIE].value if COOKIE in c else None
        if not sid:
            return None
        hid = _hash(sid)
        exp = self.svc.store.session_expires(hid)
        if not exp or exp < time.time():
            return None
        return hid

    # -- HTTP plumbing ------------------------------------------------------------------
    def _secure(self) -> bool:
        return self.svc.cfg.public_url.startswith("https://")

    def _cookie(self, sid: str, max_age: int) -> str:
        return (f"{COOKIE}={sid}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"
                + ("; Secure" if self._secure() else ""))

    def _send(self, h, code: int, body: bytes, ctype: str, extra: dict | None = None, cache: bool = False):
        h.send_response(code)
        h.send_header("Content-Type", ctype)
        h.send_header("Content-Length", str(len(body)))
        h.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
        h.send_header("Content-Security-Policy", CSP)
        h.send_header("X-Content-Type-Options", "nosniff")
        h.send_header("X-Frame-Options", "DENY")
        h.send_header("Referrer-Policy", "no-referrer")
        for k, v in (extra or {}).items():
            h.send_header(k, v)
        h.end_headers()
        h.wfile.write(body)

    def _json(self, h, obj, code: int = 200, extra: dict | None = None):
        self._send(h, code, json.dumps(obj, default=str).encode(), "application/json", extra)

    def _body(self, h) -> dict:
        n = int(h.headers.get("Content-Length") or 0)
        if n > 64_000:
            raise ValueError("request too large")
        return json.loads(h.rfile.read(n) or b"{}") if n else {}

    def _same_origin(self, h) -> bool:
        if h.headers.get("X-Debt-Agent") != "1":
            return False
        origin = h.headers.get("Origin")
        if not origin:
            return True
        allowed = {self.svc.cfg.public_url.rstrip("/")}
        host = h.headers.get("Host")
        if host:
            allowed |= {f"https://{host}", f"http://{host}"}
        return origin.rstrip("/") in allowed

    # -- routing ------------------------------------------------------------------------
    def handle(self, h, method: str) -> bool:
        """Serve the request if it belongs to the dashboard. Returns False if not ours."""
        url = urlparse(h.path)
        path, q = url.path, parse_qs(url.query)
        if not (path == "/app" or path.startswith("/app/") or path.startswith("/api/")):
            return False
        try:
            self._route(h, method, path, q)
        except (ValueError, KeyError, TypeError) as e:
            self._json(h, {"error": f"Bad request: {e}"}, 400)
        except Exception:  # noqa: BLE001
            log.exception("dashboard error on %s %s", method, path)
            self._json(h, {"error": "Something went wrong on the server. Try again in a minute."}, 500)
        return True

    def _route(self, h, method: str, path: str, q: dict):
        arg = lambda k, d=None: (q.get(k) or [d])[0]  # noqa: E731

        # Static files and login (no session needed)
        if method == "GET" and path in ASSETS:
            name, ctype = ASSETS[path]
            f = STATIC / name
            if not f.exists():
                return self._send(h, 404, b"not found", "text/plain")
            return self._send(h, 200, f.read_bytes(), ctype, cache=name != "app.html")
        if method == "GET" and path.startswith("/app/fonts/"):
            f = STATIC / "fonts" / Path(path).name
            if f.suffix != ".woff2" or not f.exists():
                return self._send(h, 404, b"not found", "text/plain")
            return self._send(h, 200, f.read_bytes(), mimetypes.types_map.get(".woff2", "font/woff2"), cache=True)
        if method == "GET" and path == "/app/login":
            token = arg("t") or ""
            if self._locked() or not self._use(self._links, token):
                self._failed()
                return self._send(h, 302, b"", "text/plain", {"Location": "/app#expired"})
            sid = self._new_session(h.headers.get("User-Agent") or "")
            return self._send(h, 302, b"", "text/plain", {"Location": "/app", "Set-Cookie": self._cookie(sid, SESSION_DAYS * 86400)})

        if method == "POST":
            if not self._same_origin(h):
                return self._json(h, {"error": "Blocked: request didn't come from the app."}, 403)
            if path == "/api/login":
                if self._locked():
                    return self._json(h, {"error": "Too many wrong codes. Wait 15 minutes, then send /app for a new one."}, 429)
                code = str(self._body(h).get("code") or "").upper().replace("-", "").replace(" ", "")
                if not self._use(self._codes, code):
                    self._failed()
                    return self._json(h, {"error": "That code didn't work. Codes last 10 minutes and work once; send /app for a new one."}, 401)
                sid = self._new_session(h.headers.get("User-Agent") or "")
                return self._json(h, {"ok": True}, extra={"Set-Cookie": self._cookie(sid, SESSION_DAYS * 86400)})

        hid = self._session(h)
        if not hid:
            return self._json(h, {"error": "signed_out"}, 401)
        self.svc.store.touch_session(hid)
        svc = self.svc

        if method == "GET":
            if path == "/api/me":
                return self._json(h, {"ok": True, "chat": self.assistant is not None})
            if path == "/api/home":
                return self._json(h, self._home())
            if path == "/api/budget":
                return self._json(h, svc.budget_status(arg("month")))
            if path == "/api/budget/suggest":
                return self._json(h, svc.budget_suggestion())
            if path == "/api/spending":
                days = max(7, min(int(arg("days", "30")), 120))
                return self._json(h, {"spending": _strip(svc.spending(days)), "subscriptions": svc.subscriptions()})
            if path == "/api/transactions":
                days = max(7, min(int(arg("days", "30")), 365))
                rows = svc.find_transactions(arg("q", "") or "", days, limit=300)
                return self._json(h, {"transactions": list(reversed(rows))})
            if path == "/api/debt":
                extra = max(0.0, min(float(arg("extra", "0")), 100_000))
                snap = svc.snapshot()
                return self._json(h, {"projection": svc.payoff_projection(extra), "debts": snap["debts"],
                                      "total_debt": snap["total_debt"]})
            return self._json(h, {"error": "not found"}, 404)

        if method == "POST":
            body = self._body(h)
            if path == "/api/logout":
                svc.store.delete_session(hid)
                return self._json(h, {"ok": True}, extra={"Set-Cookie": self._cookie("", 0)})
            if path == "/api/budget":
                limits = {str(k): (None if v in (None, "") else float(v)) for k, v in (body.get("limits") or {}).items()}
                svc.set_budget(limits, replace=bool(body.get("replace")))
                return self._json(h, svc.budget_status())
            if path.startswith("/api/subs/"):
                kind = body.get("kind")
                if kind not in ("subscription", "cancel", "bill", "ignore"):
                    raise ValueError("kind")
                svc.set_stream_kind(int(path.rsplit("/", 1)[1]), kind)
                return self._json(h, svc.subscriptions())
            if path.startswith("/api/plan/"):
                pid = int(path.split("/")[3])
                action = path.split("/")[4]
                plan = svc.store.plan(pid)
                if not plan or plan["status"] != "proposed" or action not in ("approve", "skip"):
                    raise ValueError("plan")
                svc.store.set_plan_status(pid, "approved" if action == "approve" else "skipped")
                return self._json(h, self._home())
            if path.startswith("/api/payment/") and path.endswith("/paid"):
                svc.mark_paid(int(path.split("/")[3]))
                return self._json(h, self._home())
            if path == "/api/settings":
                changes = {k: body[k] for k in ("buffer", "strategy") if k in body}
                if "strategy" in changes and changes["strategy"] not in ("avalanche", "snowball"):
                    raise ValueError("strategy")
                if "buffer" in changes:
                    changes["buffer"] = max(0.0, float(changes["buffer"]))
                svc.update_settings(**changes)
                return self._json(h, {"ok": True})
            if path == "/api/chat":
                return self._json(h, self._chat(body))
        return self._json(h, {"error": "not found"}, 404)

    # -- composed views -------------------------------------------------------------------
    def _plan_view(self, row: dict | None) -> dict | None:
        if not row:
            return None
        pays = self.svc.store.payments(row["id"])
        summary = json.loads(row["summary"])
        return {"id": row["id"], "status": row["status"], "created_at": row["created_at"],
                "deposit": summary.get("deposit"), "notes": summary.get("notes"),
                "payments": [{"id": p["id"], "name": p["name"], "amount": round((p["minimum"] or 0) + (p["extra"] or 0), 2),
                              "minimum": p["minimum"], "extra": p["extra"], "due_date": p["due_date"],
                              "pay_url": p["pay_url"], "status": p["status"], "reason": p.get("reason")}
                             for p in pays]}

    def _home(self) -> dict:
        svc = self.svc
        snap = svc.snapshot()
        b = svc.budget_status()
        latest = svc.store.latest_plan()
        plan = self._plan_view(latest) if latest and latest["status"] in ("proposed", "approved") else None
        if plan and plan["status"] == "approved" and all(p["status"] == "paid" for p in plan["payments"]):
            plan = None
        cash = round(sum(a["balance"] or 0 for a in snap["cash_accounts"]), 2)
        return {
            "snapshot": snap, "cash_total": cash, "net": round(cash - snap["total_debt"], 2),
            "budget": {k: b[k] for k in ("month", "month_name", "day", "days", "days_left", "income", "spent",
                                         "budget_total", "budgeted_spent", "left", "per_day_left", "has_budget",
                                         "debt_payments")},
            "upcoming": svc.upcoming(14), "plan": plan,
            "problems": [x for x in snap["linked_banks"] if x["status"] != "ok"],
        }

    def _chat(self, body: dict) -> dict:
        if not self.assistant:
            return {"reply": "Chat needs an Anthropic API key in your Render settings.", "actions": []}
        text = str(body.get("text") or "").strip()[:2000]
        if not text:
            raise ValueError("text")
        history = [{"role": m["role"], "content": str(m["content"])[:4000]}
                   for m in (body.get("history") or [])[-12:] if m.get("role") in ("user", "assistant")]
        reply, actions = self.assistant.ask(history, text)
        out = []
        for a in actions:
            if a["type"] == "plan":
                out.append({"type": "plan", "plan": self._plan_view(self.svc.store.plan(a["plan_id"]))})
            elif a["type"] == "confirm_settings":
                out.append(a)
        return {"reply": reply, "actions": out}


def _strip(sp: dict) -> dict:
    for c in sp.get("categories", []):
        c.pop("key", None)
    return sp
