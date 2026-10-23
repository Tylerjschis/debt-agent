"""Connect banks from your own computer (alternative to /link in Telegram).

Run:  python scripts/link_accounts.py
It prints a link; open it, click Connect, and log in through Plaid's window.
Use the same DEBTBOT_DB and DEBTBOT_KEY as the bot. Once the bot runs in the
cloud, use /link instead so the tokens land in the cloud database.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from debtbot.config import Config  # noqa: E402
from debtbot.service import Service  # noqa: E402
from debtbot.store import Store  # noqa: E402
from debtbot.web import LinkServer  # noqa: E402

if __name__ == "__main__":
    cfg = Config()
    svc = Service(cfg, Store(cfg.db_path, cfg.db_key))
    web = LinkServer(svc, cfg.port, cfg.plaid_redirect_uri)
    web.start()
    print(f"Open http://localhost:{cfg.port}/link?t={web.new_token()}  (works for 15 minutes; Ctrl+C to stop)")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
