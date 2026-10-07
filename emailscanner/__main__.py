"""Command line:

  python -m emailscanner auth --client-secret client_secret.json   # once, on your PC
  python -m emailscanner init-sheet                                # once, creates the Google Sheet
  python -m emailscanner scan-backlog                              # reads every sender into the sheet
  python -m emailscanner apply                                     # acts on your Delete/Keep choices
  python -m emailscanner sweep [--limit N] [--clean]               # auto-clean preview (or do it)
  python -m emailscanner digest [--force] [--preview]              # the scheduled run
"""
from __future__ import annotations

import argparse
import logging
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="emailscanner")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("auth", help="One-time Google sign-in (run on your computer)")
    a.add_argument("--client-secret", default="client_secret.json")
    sub.add_parser("init-sheet", help="Create the Google Sheet and print its ID")
    sub.add_parser("scan-backlog", help="Group all mail by sender into the review tab")
    ap_apply = sub.add_parser("apply", help="Act on decisions in the review tab")
    ap_apply.add_argument("--limit", type=int, default=None)
    sw = sub.add_parser("sweep", help="Dry run: propose delete/review/keep for every remaining email")
    sw.add_argument("--limit", type=int, default=None, help="Only look at the newest N emails")
    sw.add_argument("--clean", action="store_true", help="Actually move the obvious junk to Trash")
    sw.add_argument("--max-checks", type=int, default=None, help="Max emails Claude reads (cost cap)")
    d = sub.add_parser("digest", help="Rate new mail and send the digest")
    d.add_argument("--force", action="store_true", help="Run now even if it's not a digest time")
    d.add_argument("--preview", action="store_true",
                   help="Write digest_preview.html only: no labels, no sheet writes, no email")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        datefmt="%H:%M:%S", stream=sys.stdout)
    logging.getLogger("googleapiclient").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    from .config import load_config
    cfg = load_config()

    if args.cmd == "auth":
        from .auth import run_local_auth
        run_local_auth(args.client_secret)
        return 0

    from .auth import get_credentials
    creds = get_credentials()

    if args.cmd == "init-sheet":
        from .sheets import Sheet
        sid = Sheet.create(creds)
        print(f"\nCreated your sheet: https://docs.google.com/spreadsheets/d/{sid}/edit")
        print(f"Add this GitHub secret:\nSHEET_ID = {sid}")
        return 0

    from .gmail import Gmail
    from .sheets import Sheet
    if args.cmd == "scan-backlog":
        from .backlog import scan
        scan(Gmail(creds), Sheet(creds, cfg.sheet_id))
    elif args.cmd == "apply":
        from .backlog import apply_decisions
        s = apply_decisions(Gmail(creds), Sheet(creds, cfg.sheet_id), limit=args.limit)
        print(f"Processed {s['senders']} senders, trashed {s['trashed']:,} emails.")
        for m in s["manual_unsubs"]:
            print(f"  finish unsubscribing: {m['sender']} -> {m['url']}")
    elif args.cmd == "sweep":
        from .sweep import run_sweep
        r = run_sweep(Gmail(creds), Sheet(creds, cfg.sheet_id), cfg=cfg, limit=args.limit,
                      clean=args.clean, max_checks=args.max_checks)
        print(r["actions"], f"cleaned={r['cleaned']}" if args.clean else "(preview only)")
    elif args.cmd == "digest":
        from .digest import run
        run(cfg, force=args.force, preview=args.preview)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
