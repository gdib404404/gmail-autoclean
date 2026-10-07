"""Backlog cleanup: scan every sender into the review sheet, then act on your decisions."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from . import sheets as S
from .gmail import Gmail
from .senders import (FREEMAIL, SECURITY_SUBJECT, addresses, aggregate, base_domain, domain_of,
                      is_account_domain, merge_review, parse_message)
from .unsubscribe import execute as do_unsub
from .unsubscribe import plan as unsub_plan

log = logging.getLogger(__name__)

DELETE_DECISIONS = {"Delete", "Delete (no unsubscribe)", "Delete whole domain"}


def _progress(label):
    last = {"n": -1}

    def fn(done, total=None):
        step = done // 2000
        if step != last["n"]:
            last["n"] = step
            log.info("%s: %s%s", label, f"{done:,}", f" / {total:,}" if total else "")
    return fn


def replied_addresses(gmail: Gmail) -> set[str]:
    ids = gmail.list_ids("in:sent", progress=_progress("sent mail listed"))
    msgs = gmail.get_batch(ids, headers=["To", "Cc"], progress=_progress("sent mail read"))
    out: set[str] = set()
    for m in msgs.values():
        p = parse_message(m)
        out.update(addresses(p["to"]))
        out.update(addresses(p["cc"]))
    return out


def scan(gmail: Gmail, sheet: S.Sheet, query: str = "-in:sent -in:chats -in:drafts") -> int:
    """Read headers of every message and write one row per sender to the review tab."""
    sheet.ensure_tabs()
    me = gmail.me()
    log.info("Collecting people you've emailed...")
    replied = replied_addresses(gmail)
    log.info("Listing mail...")
    ids = gmail.list_ids(query, progress=_progress("messages listed"))
    log.info("Reading headers for %s messages (this takes a while for big inboxes)", f"{len(ids):,}")
    raw = gmail.get_batch(ids, progress=_progress("headers read"))
    parsed = [parse_message(m) for m in raw.values()]
    fresh = aggregate(parsed, replied, me=me)
    merged = merge_review(sheet.read(S.REVIEW), fresh)
    sheet.replace_all(S.REVIEW, merged)
    sheet.format_tabs()
    sheet.set_state(last_scan=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    last_scan_messages=len(parsed))
    log.info("Wrote %d senders (%s messages) to '%s'", len(fresh), f"{len(parsed):,}", S.REVIEW)
    return len(fresh)


def important_matchers(sheet: S.Sheet) -> set[str]:
    return {r.get("Sender or domain", "").strip().lower()
            for r in sheet.read(S.IMPORTANT) if r.get("Sender or domain", "").strip()}


def is_protected(addr: str, protected: set[str]) -> bool:
    addr = addr.lower()
    return addr in protected or domain_of(addr) in protected or base_domain(addr) in protected


def is_finished(row: dict) -> bool:
    st = row.get("Status", "")
    return st.startswith("Done") or st.startswith("Covered")


def tidy_review(sheet: S.Sheet) -> int:
    """Move deleted senders to the 'Deleted Senders' tab and re-sort what's left.

    Review Senders order: undecided / Later (biggest first), then Skipped rows
    that need a new decision, then everything you chose to Keep at the bottom.
    Returns how many rows were moved.
    """
    from .senders import to_int

    sheet.ensure_tabs()
    rows = sheet.read(S.REVIEW)
    done = [r for r in rows if is_finished(r)]
    remaining = [r for r in rows if not is_finished(r) and r.get("Sender")]
    for r in remaining:
        if r.get("Status", "").startswith("Skipped") and r.get("Decision") in DELETE_DECISIONS:
            r["Decision"] = ""  # needs a new choice from you

    def rank(r):
        if r.get("Decision") == "Keep" or r.get("Status") == "Kept":
            group = 2
        elif r.get("Status", "").startswith("Skipped"):
            group = 1
        else:
            group = 0
        return (group, -to_int(r.get("Emails")))

    remaining.sort(key=rank)
    if done:
        sheet.append(S.ARCHIVE, done)
    order_changed = [r.get("Sender") for r in rows if not is_finished(r)] != [r.get("Sender") for r in remaining]
    if done or order_changed:
        sheet.replace_all(S.REVIEW, remaining)
    if done:
        log.info("Moved %d deleted senders to '%s'", len(done), S.ARCHIVE)
    return len(done)


def apply_decisions(gmail: Gmail, sheet: S.Sheet, limit: int | None = None, flush_every: int = 25) -> dict:
    """Act on every 'Delete*' decision that hasn't been processed yet.

    For each: trash all mail from the sender, unsubscribe (unless told not to),
    and add a Gmail filter that sends future mail straight to Trash.
    """
    from zoneinfo import ZoneInfo

    from .config import load_config

    rows = sheet.read(S.REVIEW)
    protected = important_matchers(sheet)
    # Senders you chose to Keep are never swept up by a whole-domain delete.
    protected |= {r["Sender"].strip().lower() for r in rows if r.get("Decision") == "Keep" and r.get("Sender")}
    # "Skipped" rows get their Decision cleared by tidy_review(); if you pick a new
    # decision for one, it's picked up again.
    todo = [r for r in rows if r.get("Decision") in DELETE_DECISIONS
            and (not r.get("Status") or r["Status"].startswith("Skipped"))]
    kept = [r for r in rows if r.get("Decision") == "Keep" and not r.get("Status")]
    if limit:
        todo = todo[:limit]
    summary = {"senders": 0, "trashed": 0, "manual_unsubs": []}
    now = datetime.now(ZoneInfo(load_config().timezone)).strftime("%Y-%m-%d %H:%M")

    updates: list[tuple[int, dict]] = [(r["_row"], {"Status": "Kept", "Processed at": now}) for r in kept]
    # rows waiting for their filter: (row, target, notes, excludes)
    waiting: list[tuple[dict, str, list[str], list[str]]] = []

    def flush():
        if waiting:
            notes = gmail.create_delete_filters(
                [t for _, t, _, _ in waiting],
                domain_excludes={t: ex for _, t, _, ex in waiting if ex})
            for row, t, n, _ in waiting:
                updates.append((row["_row"], {"Status": "Done: " + " · ".join(n + [notes.get(t, "")]).strip(" ·"),
                                              "Processed at": now}))
            waiting.clear()
        sheet.update_many(S.REVIEW, updates)
        updates.clear()

    def skip(r, status):
        r["Status"] = status
        updates.append((r["_row"], {"Status": status, "Processed at": now}))

    def save_state():
        if summary["senders"] or summary["manual_unsubs"]:
            pending = sheet.get_state("pending_manual_unsubs", []) or []
            stats = sheet.get_state("cleanup_since_digest", {"senders": 0, "trashed": 0}) or {}
            sheet.set_state(
                pending_manual_unsubs=pending + summary["manual_unsubs"],
                cleanup_since_digest={"senders": stats.get("senders", 0) + summary["senders"],
                                      "trashed": stats.get("trashed", 0) + summary["trashed"]},
            )

    done_domains: set[str] = set()
    try:
        for n, r in enumerate(todo, start=1):
            if r.get("Status", "").startswith("Covered"):  # handled by a domain delete earlier in this run
                continue
            addr = r["Sender"].strip().lower()
            decision = r["Decision"]
            whole_domain = decision == "Delete whole domain"
            target = base_domain(addr) if whole_domain else addr

            if whole_domain and target in FREEMAIL:
                skip(r, f"Skipped: can't delete all of {target} (that's everyone on it). Use 'Delete' for just this sender.")
                continue
            if whole_domain and is_account_domain(target):
                skip(r, f"Skipped: {target} sends logins/codes/receipts, so whole-domain delete is blocked. "
                        "Use 'Delete' on its marketing senders instead.")
                continue
            if (not whole_domain and is_protected(addr, protected)) or (whole_domain and target in protected):
                skip(r, "Skipped: sender is on your Always Important list")
                continue
            if whole_domain and target in done_domains:
                skip(r, f"Covered by {target} delete")
                continue

            ids = gmail.list_ids(f"from:{target}")
            newest_meta = None
            auto_kept: set[str] = set()
            if ids:
                metas = gmail.get_batch(ids if whole_domain else ids[:10])
                if whole_domain:
                    # Gmail's from: search is fuzzy with domains; only keep true matches.
                    # Any address at the domain that has sent a login/security/receipt-type
                    # email is kept, and left out of the filter.
                    parsed = {mid: parse_message(metas[mid]) for mid in ids if mid in metas}
                    for p in parsed.values():
                        if base_domain(p["from_email"]) == target and SECURITY_SUBJECT.search(p["subject"]):
                            auto_kept.add(p["from_email"])
                    ids = [mid for mid, p in parsed.items()
                           if base_domain(p["from_email"]) == target
                           and p["from_email"] not in auto_kept
                           and not is_protected(p["from_email"], protected)]
                # newest message that actually carries an unsubscribe header
                newest_meta = next((metas[i] for i in ids[:10] if i in metas
                                    and parse_message(metas[i])["list_unsubscribe"]), None)
            if newest_meta is None and decision != "Delete (no unsubscribe)" and not whole_domain:
                # e.g. mail already trashed by an earlier run that got cut off
                t_ids = gmail.list_ids(f"in:trash from:{target}", limit=5)
                if t_ids:
                    tm = gmail.get_batch(t_ids)
                    newest_meta = next((tm[i] for i in t_ids if i in tm
                                        and parse_message(tm[i])["list_unsubscribe"]), None)

            trashed = gmail.trash(ids)
            notes = [f"trashed {trashed:,}"]
            if auto_kept:
                notes.append(f"kept {len(auto_kept)} login/receipt sender(s): " + ", ".join(sorted(auto_kept)[:5]))

            if decision == "Delete (no unsubscribe)":
                notes.append("no unsubscribe (your call)")
            elif newest_meta:
                p = parse_message(newest_meta)
                pl = unsub_plan(p["list_unsubscribe"], p["list_unsubscribe_post"])
                ok, note = do_unsub(pl, gmail)
                notes.append(note)
                if not ok and pl.get("url"):
                    summary["manual_unsubs"].append({"sender": target, "url": pl["url"]})
            else:
                notes.append("no unsubscribe offered")

            excludes = sorted({x for x in protected if "@" in x and base_domain(x) == target} | auto_kept) \
                if whole_domain else []
            waiting.append((r, target, notes, excludes))
            if whole_domain:
                done_domains.add(target)
                for other in rows:
                    if (other is not r and not other.get("Status") and other.get("Decision") != "Keep"
                            and other.get("Sender", "").lower() not in auto_kept
                            and base_domain(other.get("Sender", "")) == target):
                        skip(other, f"Covered by {target} delete")
            summary["senders"] += 1
            summary["trashed"] += trashed
            log.info("%s -> %s", target, " · ".join(notes))
            if n % flush_every == 0:
                flush()
    finally:
        # Even if something breaks mid-run, record what was already done.
        try:
            flush()
        finally:
            save_state()
    tidy_review(sheet)
    return summary
