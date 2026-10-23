"""Command-line helpers.

  python -m debtbot.cli genkey               make an encryption key for .env
  python -m debtbot.cli link-sandbox         link Plaid's fake bank (Sandbox)
  python -m debtbot.cli sync                 pull fresh data now
  python -m debtbot.cli status               balances, rates, due dates
  python -m debtbot.cli plan [--deposit N]   what you'd pay right now
  python -m debtbot.cli compare [--extra N]  avalanche vs snowball
"""
from __future__ import annotations

import argparse
import re

from .config import Config
from .messages import compare_text, plan_text, status_text
from .service import Service
from .store import Store


def plain(html: str) -> str:
    return re.sub(r"</?[bi]>", "", html).replace("&amp;", "&").replace("&#x27;", "'")


def main():
    ap = argparse.ArgumentParser(prog="debtbot")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("genkey")
    sub.add_parser("link-sandbox")
    sub.add_parser("sync")
    sub.add_parser("status")
    p = sub.add_parser("plan")
    p.add_argument("--deposit", type=float, default=0.0, help="pretend a paycheck of this size just landed")
    c = sub.add_parser("compare")
    c.add_argument("--extra", type=float, default=500.0)
    args = ap.parse_args()

    if args.cmd == "genkey":
        from cryptography.fernet import Fernet
        print(Fernet.generate_key().decode())
        return

    cfg = Config()
    svc = Service(cfg, Store(cfg.db_path, cfg.db_key))

    if args.cmd == "link-sandbox":
        item = svc.link_sandbox()
        svc.refresh()
        print(f"Linked Plaid Sandbox bank (item {item}). Accounts:")
        for a in svc.store.accounts():
            print(f"  {a['type']:<11} {a['name']:<32} ${a['balance']:,.2f}")
        return

    broken = svc.refresh()
    for b in broken:
        print(f"! {b} needs you to log in again")
    if args.cmd == "sync":
        print(f"Synced {len(svc.store.accounts())} accounts.")
    elif args.cmd == "status":
        print(plain(status_text(svc.snapshot())))
    elif args.cmd == "plan":
        if args.deposit:
            svc.fake_paycheck(args.deposit)
        _, pid = svc.build_plan(deposit=args.deposit)
        print(plain(plan_text(svc.store.plan(pid), svc.store.payments(pid))))
    elif args.cmd == "compare":
        print(plain(compare_text(svc.compare(args.extra), args.extra)))


if __name__ == "__main__":
    main()
