"""Tiny web server for connecting banks through Plaid Link, from any browser.

Runs inside the bot process. /link in Telegram sends you a one-time URL that
works for 15 minutes. Without that token every page returns 404, so the
server has nothing to offer anyone else. It also answers GET /health for
the host's health check.
"""
from __future__ import annotations

import json
import logging
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import plaid_sync
from .service import Service

log = logging.getLogger(__name__)

TOKEN_TTL = 15 * 60

PAGE = """<!doctype html><html lang=en><head><meta charset=utf-8>
<meta name=viewport content="width=device-width, initial-scale=1">
<title>Connect a bank</title>
<style>
:root{color-scheme:light dark;--bg:#fff;--fg:#111;--muted:#666;--accent:#1d4ed8}
@media (prefers-color-scheme:dark){:root{--bg:#111;--fg:#eee;--muted:#999;--accent:#60a5fa}}
body{font:17px/1.5 system-ui,sans-serif;background:var(--bg);color:var(--fg);max-width:480px;margin:40px auto;padding:0 16px}
p{color:var(--muted)}
button{font:inherit;font-weight:600;width:100%;padding:14px;border-radius:10px;border:0;background:var(--accent);color:#fff}
#log{margin-top:20px;white-space:pre-line}
</style></head><body>
<h1>Connect a bank</h1>
<p>Pick your bank and log in through Plaid's secure window. One login covers every account at that bank. Your password goes only to Plaid and your bank.</p>
<p>__SANDBOX__</p>
<button id=go>Connect a bank</button>
<div id=log></div>
<script src="https://cdn.plaid.com/link/v2/stable/link-initialize.js"></script>
<script>
const T = new URLSearchParams(location.search).get('t') || sessionStorage.getItem('dbt');
if (T) sessionStorage.setItem('dbt', T);
const log = m => document.getElementById('log').textContent += m + '\\n';
function openLink(token, redirect) {
  const cfg = {token,
    onSuccess: async (public_token, meta) => {
      log('Saving...');
      const r = await fetch('/exchange?t=' + encodeURIComponent(T), {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({public_token, name: meta.institution ? meta.institution.name : 'Bank'})});
      const j = await r.json();
      log(j.ok ? 'Connected: ' + j.name + '. Connect another, or go back to Telegram and send /status.' : 'Failed: ' + j.error);
    },
    onExit: err => { if (err) log('Stopped: ' + (err.display_message || err.error_message || err.error_code)); }};
  if (redirect) cfg.receivedRedirectUri = location.href;
  Plaid.create(cfg).open();
}
document.getElementById('go').onclick = async () => {
  const r = await fetch('/link_token?t=' + encodeURIComponent(T), {method:'POST'});
  if (!r.ok) return log('This link expired. Send /link in Telegram for a new one.');
  const {link_token} = await r.json();
  sessionStorage.setItem('dbl', link_token);
  openLink(link_token, false);
};
if (location.pathname === '/oauth' && sessionStorage.getItem('dbl')) openLink(sessionStorage.getItem('dbl'), true);
</script></body></html>"""


class LinkServer:
    def __init__(self, svc: Service, port: int, redirect_uri: str | None = None):
        self.svc = svc
        self.port = port
        self.redirect_uri = redirect_uri
        self._tokens: dict[str, float] = {}
        self._lock = threading.Lock()

    def new_token(self) -> str:
        t = secrets.token_urlsafe(18)
        with self._lock:
            now = time.time()
            self._tokens = {k: v for k, v in self._tokens.items() if v > now}
            self._tokens[t] = now + TOKEN_TTL
        return t

    def valid(self, t: str | None) -> bool:
        with self._lock:
            return bool(t) and self._tokens.get(t, 0) > time.time()

    def start(self):
        server = ThreadingHTTPServer(("0.0.0.0", self.port), self._handler())
        threading.Thread(target=server.serve_forever, daemon=True, name="link-server").start()
        log.info("Link server listening on port %s", self.port)
        return server

    def _handler(self):
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code: int, body: bytes, ctype: str):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj, code=200):
                self._send(code, json.dumps(obj).encode(), "application/json")

            def _token(self) -> str | None:
                return (parse_qs(urlparse(self.path).query).get("t") or [None])[0]

            def do_GET(self):
                path = urlparse(self.path).path
                if path == "/health":
                    return self._send(200, b"ok", "text/plain")
                # /oauth is Plaid returning from a bank's own login page; the page
                # carries its token in session storage, and every API call is checked.
                if (path == "/link" and outer.valid(self._token())) or path == "/oauth":
                    note = ("Sandbox: log in with username user_good and password pass_good."
                            if outer.svc.cfg.plaid_env == "sandbox" else "")
                    return self._send(200, PAGE.replace("__SANDBOX__", note).encode(), "text/html; charset=utf-8")
                self._send(404, b"not found", "text/plain")

            def do_POST(self):
                path = urlparse(self.path).path
                if not outer.valid(self._token()):
                    return self._json({"error": "expired"}, 404)
                api = outer.svc.plaid
                if api is None:
                    return self._json({"error": "Plaid isn't set up (mode: plaid and keys in settings)."}, 400)
                try:
                    if path == "/link_token":
                        return self._json({"link_token": plaid_sync.create_link_token(api, redirect_uri=outer.redirect_uri)})
                    if path == "/exchange":
                        data = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                        name = str(data.get("name") or "Bank")[:80]
                        plaid_sync.exchange_public_token(api, outer.svc.store, data["public_token"], name)
                        threading.Thread(target=outer.svc.refresh, daemon=True).start()
                        return self._json({"ok": True, "name": name})
                except Exception as e:  # noqa: BLE001
                    log.exception("link step failed")
                    return self._json({"ok": False, "error": str(e)[:200]}, 500)
                self._json({"error": "not found"}, 404)

            def log_message(self, fmt, *args):
                log.debug("web: " + fmt, *args)

        return H
