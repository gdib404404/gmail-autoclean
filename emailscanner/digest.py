"""The scheduled run: apply sender decisions, rate new mail, label it, update trackers, send digest."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from . import render
from . import sheets as S
from .auth import get_credentials
from .backlog import apply_decisions, important_matchers, is_protected
from .classify import classify
from .config import ROOT, Config
from .gmail import Gmail, extract_body, has_attachment, message_id
from .schedule import current_slot, slot_label
from .senders import base_domain, iso_day, looks_spammy, parse_message
from .unsubscribe import plan as unsub_plan

log = logging.getLogger(__name__)

TIER_LABEL_KEY = {"priority": "priority", "worth_a_look": "worth_a_look", "low": "low", "junk": "junk"}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def is_own_digest(m: dict, me: str) -> bool:
    return m["from_email"] == me and m["subject"].startswith("[Digest]")


# ---------------------------------------------------------------- learning
def learn_from_corrections(gmail: Gmail, sheet: S.Sheet, cfg: Config, now: datetime) -> int:
    """If you add !Priority to something rated lower (or remove it from something rated
    priority), remember that sender so future ratings shift."""
    rows = sheet.read(S.LOG)
    cutoff = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    cand = [r for r in rows if not r.get("Correction") and r.get("Rated at", "")[:10] >= cutoff and r.get("Message ID")]
    if not cand:
        return 0
    prio = gmail.label_id(cfg.label("priority"))
    metas = gmail.get_batch([r["Message ID"] for r in cand], headers=["From"])
    learned = []
    for r in cand:
        m = metas.get(r["Message ID"])
        if not m or "TRASH" in (m.get("labelIds") or []):
            continue
        has = prio in (m.get("labelIds") or [])
        direction = "up" if (r["Tier"] != "priority" and has) else "down" if (r["Tier"] == "priority" and not has) else ""
        if not direction:
            continue
        sheet.update_cells(S.LOG, r["_row"], {"Correction": direction})
        learned.append({"Sender": r["Sender"], "Direction": direction, "Example subject": r.get("Subject", ""),
                        "Date": now.strftime("%Y-%m-%d")})
    sheet.append(S.LEARNED, learned, raw=True)
    if learned:
        log.info("Learned from %d corrections", len(learned))
    return len(learned)


def prune_log(sheet: S.Sheet, now: datetime, keep_days: int = 14) -> None:
    rows = sheet.read(S.LOG)
    cutoff = (now - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    kept = [r for r in rows if r.get("Rated at", "")[:10] >= cutoff]
    if len(kept) < len(rows):
        sheet.replace_all(S.LOG, kept, raw=True)


# ---------------------------------------------------------------- trackers
def update_jobs(sheet: S.Sheet, items: list[dict], today: str) -> None:
    jobs = [i for i in items if i.get("job")]
    if not jobs:
        return
    rows = sheet.read(S.JOBS)
    new_rows = []
    for it in sorted(jobs, key=lambda i: i["date_ms"]):
        j = it["job"]
        company = (j.get("company") or it.get("from_name") or base_domain(it["from_email"])).strip()
        role = (j.get("role") or "").strip()
        status = render.JOB_STATUS.get(j.get("status"), "Update")
        link = S.hyperlink(render.thread_link(it["thread_id"]), "open")
        match = None
        for r in rows + new_rows:
            if _norm(r.get("Company")) == _norm(company) and (
                    _norm(r.get("Role")) == _norm(role) or not role or not r.get("Role")):
                match = r
                break
        vals = {"Status": status, "Last update": today, "Latest note": it.get("summary", ""), "Email link": link}
        if match and "_row" in match:
            if role and not match.get("Role"):
                vals["Role"] = role
            sheet.update_cells(S.JOBS, match["_row"], vals)
            match.update(vals)
        elif match:
            match.update(vals)
        else:
            new_rows.append({"Company": company, "Role": role, "First seen": today, **vals})
    sheet.append(S.JOBS, new_rows)


def collect_events(items: list[dict], cfg: Config) -> list[dict]:
    out, seen = [], set()
    for it in items:
        ev = it.get("event")
        if not ev or not ev.get("start") or it["tier"] == "junk":
            continue
        key = (_norm(ev.get("title")), ev.get("start"))
        if key in seen:
            continue
        seen.add(key)
        out.append({"title": ev.get("title") or it["subject"], "when": render.human_when(ev),
                    "where": ev.get("location") or "", "thread_id": it["thread_id"],
                    "cal": render.calendar_link(ev, cfg.timezone,
                                                details=f"From email: {it['subject']}\n{render.thread_link(it['thread_id'])}")})
    return out


def save_events(sheet: S.Sheet, events: list[dict], today: str) -> None:
    if not events:
        return
    existing = {(_norm(r.get("Title")), r.get("When")) for r in sheet.read(S.EVENTS)}
    sheet.append(S.EVENTS, [{
        "Found": today, "Title": e["title"], "When": e["when"], "Where": e["where"],
        "Add to calendar": S.hyperlink(e["cal"], "add") if e["cal"] else "",
        "Email link": S.hyperlink(render.thread_link(e["thread_id"]), "open"),
    } for e in events if (_norm(e["title"]), e["when"]) not in existing])


def save_bills(sheet: S.Sheet, items: list[dict], today: str) -> None:
    sheet.append(S.BILLS, [{
        "Date": today, "Vendor": i["bill"].get("vendor") or i.get("from_name") or i["from_email"],
        "Type": (i["bill"].get("kind") or "").title(), "Amount": i["bill"].get("amount") or "",
        "Due": i["bill"].get("due") or "", "Email link": S.hyperlink(render.thread_link(i["thread_id"]), "open"),
    } for i in items if i.get("bill")])


def queue_new_senders(sheet: S.Sheet, items: list[dict], protected: set[str]) -> int:
    """Senders rated 'junk' that you haven't reviewed yet go into the review tab."""
    junk = [i for i in items if i["tier"] == "junk"]
    if not junk:
        return 0
    known = {r.get("Sender", "").lower() for r in sheet.read(S.REVIEW) + sheet.read(S.ARCHIVE)}
    by_sender: dict[str, list[dict]] = {}
    for i in junk:
        a = i["from_email"]
        if a and a not in known and not is_protected(a, protected):
            by_sender.setdefault(a, []).append(i)
    rows = []
    for a, msgs in by_sender.items():
        m = max(msgs, key=lambda x: x["date_ms"])
        kind = unsub_plan(m["list_unsubscribe"], m["list_unsubscribe_post"])["method"]
        rows.append({"Decision": "", "Sender": a, "Name": m.get("from_name", ""), "Domain": base_domain(a),
                     "Emails": len(msgs), "Unread": sum(1 for x in msgs if x["unread"]),
                     "Last email": iso_day(m["date_ms"]), "First email": iso_day(min(x["date_ms"] for x in msgs)),
                     "Sample subjects": " | ".join(dict.fromkeys(x["subject"][:90] for x in msgs[:3])),
                     "Unsubscribe": kind,
                     "Looks spammy?": "Maybe" if looks_spammy(a, m.get("from_name", ""), kind != "none", False) else "",
                     "Source": "digest"})
    sheet.append(S.REVIEW, rows)
    return len(rows)


# ---------------------------------------------------------------- main run
def run(cfg: Config, force: bool = False, preview: bool = False) -> None:
    tz = ZoneInfo(cfg.timezone)
    now = datetime.now(tz)
    slot = current_slot(now, cfg.digest_hours)
    if not slot and not (force or preview):
        log.info("%s isn't a digest time (%s). Nothing to do.", now.strftime("%H:%M"), cfg.digest_hours)
        return
    slot = slot or f"{now:%Y-%m-%d}@{now.hour:02d}"

    creds = get_credentials()
    gmail = Gmail(creds)
    sheet = S.Sheet(creds, cfg.sheet_id)
    if not preview:
        sheet.ensure_tabs()
    if not (force or preview) and sheet.get_state("last_slot") == slot:
        log.info("Digest for %s already sent.", slot)
        return

    # 1) act on anything you marked in the review sheet
    if not preview:
        s = apply_decisions(gmail, sheet)
        if s["senders"]:
            log.info("Applied %d sender decisions (%d emails trashed)", s["senders"], s["trashed"])
        learn_from_corrections(gmail, sheet, cfg, now)
    from . import sweep as AC
    recovered = [] if preview else AC.detect_recoveries(gmail, sheet)

    # 2) new mail since last run
    me = gmail.me()
    seen_label = gmail.label_id(cfg.label("seen"))
    last_epoch = sheet.get_state("last_run_epoch")
    since = int(last_epoch) - 3600 if last_epoch else int((now - timedelta(hours=24)).timestamp())
    ids = gmail.list_ids(f"in:inbox after:{since}", limit=cfg.max_emails_per_run * 4)
    metas = gmail.get_batch(ids)
    msgs = [parse_message(m) for m in metas.values()]
    msgs = [m for m in msgs if seen_label not in m["labels"] and not is_own_digest(m, me)]
    msgs.sort(key=lambda m: m["date_ms"])
    overflow = len(msgs) > cfg.max_emails_per_run
    msgs = msgs[:cfg.max_emails_per_run]
    log.info("%d new emails to rate%s", len(msgs), " (capped, rest next run)" if overflow else "")

    if msgs:
        full = gmail.get_batch([m["id"] for m in msgs], fmt="full")
        replied_cache: dict[str, bool] = {}
        for m in msgs:
            fm = full.get(m["id"], {})
            m["body"] = extract_body(fm, cfg.body_chars) or m["snippet"]
            m["has_attachment"] = has_attachment(fm)
            m["message_id"] = message_id(fm)
            a = m["from_email"]
            if a not in replied_cache:
                replied_cache[a] = bool(gmail.list_ids(f"in:sent to:{a}", limit=1)) if a else False
            m["replied_before"] = replied_cache[a]

    # 3) rate
    protected = important_matchers(sheet)
    learned = sheet.read(S.LEARNED)[-300:]
    ratings = classify(msgs, cfg, tz, learned, protected)
    items = [{**m, **r} for m, r in zip(msgs, ratings)]

    # 3b) auto-clean obvious junk (only when turned on in config.yaml)
    auto_deleted: list[dict] = []
    if (cfg.auto_clean or preview) and items:
        replied, my_threads = AC.sent_context(gmail, "in:sent newer_than:1y")
        ctx_ac, _ = AC.build_context(sheet, items, replied, my_threads, me, cfg.auto_clean_min_certainty,
                                     0, cfg.never_auto_delete)
        for it in items:
            action, conf, why = AC.decide(it, ctx_ac, verdict=it)
            if action == "delete":
                it["why"] = why
                auto_deleted.append(it)
        log.info("Auto-clean: %d of %d new emails are obvious junk", len(auto_deleted), len(items))

    today = now.strftime("%Y-%m-%d")
    events = collect_events([i for i in items if i not in auto_deleted], cfg)

    new_review = 0
    if not preview and items:
        # 4) label in Gmail
        by_tier: dict[str, list[str]] = {}
        for it in items:
            by_tier.setdefault(it["tier"], []).append(it["id"])
        for tier, tids in by_tier.items():
            add = [gmail.label_id(cfg.label(TIER_LABEL_KEY[tier])), seen_label]
            remove = []
            if tier == "priority" and cfg.star_priority:
                add.append("STARRED")
            if (tier == "low" and cfg.archive_low) or (tier == "junk" and cfg.archive_junk):
                remove.append("INBOX")
            gmail.modify(tids, add=add, remove=remove)

        # 5) log + trackers
        stamp = now.strftime("%Y-%m-%d %H:%M")
        sheet.append(S.LOG, [{"Message ID": i["id"], "Thread ID": i["thread_id"], "Sender": i["from_email"],
                              "Subject": i["subject"][:200], "Tier": i["tier"], "Rated at": stamp} for i in items], raw=True)
        update_jobs(sheet, items, today)
        save_events(sheet, events, today)
        save_bills(sheet, items, today)
        new_review = queue_new_senders(sheet, items, protected)
        AC.auto_clean(gmail, sheet, auto_deleted, "digest")

    shown = [i for i in items if i not in auto_deleted]
    if preview and auto_deleted:
        preview_auto = {"count": len(auto_deleted), "items": [
            {"sender": m.get("from_name") or m["from_email"], "email": m["from_email"],
             "subject": m["subject"][:120], "why": m["why"], "link": AC.trash_search_link(m),
             "source": "preview"} for m in auto_deleted]}
    else:
        preview_auto = None

    # 6) digest
    manual = sheet.get_state("pending_manual_unsubs", []) or []
    cleanup = sheet.get_state("cleanup_since_digest", {}) or {}
    auto = preview_auto or sheet.get_state("auto_cleaned_since_digest", {}) or {}
    ctx = {"slot_label": slot_label(slot), "date_label": now.strftime("%A, %B %d"),
           "events": events, "manual_unsubs": manual, "cleanup": cleanup,
           "new_review_senders": new_review, "sheet_url": sheet.url,
           "auto_cleaned": auto, "recovered": recovered}
    subject, html, text = render.build(shown, ctx)

    worth_sending = any(i["tier"] in ("priority", "worth_a_look") or i.get("job") or i.get("bill") for i in shown) \
        or events or manual or cleanup.get("senders") or new_review or auto.get("count") or recovered

    if preview:
        out = Path(ROOT / "digest_preview.html")
        out.write_text(html, encoding="utf-8")
        log.info("Preview written to %s (nothing sent, nothing labeled). Subject: %s", out, subject)
        return

    if worth_sending or cfg.send_empty_digest:
        sent = gmail.send(me, subject, text, html)
        gmail.modify([sent["id"]], add=[seen_label])
        log.info("Sent: %s", subject)
    else:
        log.info("Nothing worth a digest this time; skipped sending.")

    new_epoch = int(msgs[-1]["date_ms"] / 1000) if (overflow and msgs) else int(now.timestamp())
    sheet.set_state(last_slot=slot, last_run_epoch=new_epoch, pending_manual_unsubs=[],
                    cleanup_since_digest={"senders": 0, "trashed": 0},
                    auto_cleaned_since_digest={"count": 0, "items": []},
                    last_run=now.isoformat(timespec="seconds"))
    prune_log(sheet, now)
