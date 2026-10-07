"""Dry-run "auto-clean" sweep.

Looks at every email still in the mailbox and proposes Delete / Review / Keep for
each one, with a confidence and a reason. It learns from the decisions you already
made in the sheet (Deleted Senders = delete, Keep = keep). Changes nothing in Gmail.

Results go to two sheet tabs:
  Sweep Preview  - one row per sender + proposed action (mixed senders get several rows)
  Sweep Summary  - totals, how accurate the learned model is on your past decisions,
                   and the things most likely to cause mistakes
"""
from __future__ import annotations

import logging
import math
import re
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from urllib.parse import quote

from . import sheets as S
from .gmail import Gmail
from .senders import (FREEMAIL, addresses, aggregate, base_domain, domain_of, is_account_domain,
                      parse_message, to_int)

log = logging.getLogger(__name__)

DAY_MS = 86_400_000

# ---------------------------------------------------------------- subject patterns
TRANSACTIONAL = re.compile(
    r"(?i)\b(order|receipt|invoice|payment (received|confirmation|scheduled|due)|your payment|"
    r"shipped|shipping|delivered|delivery|out for delivery|tracking|arriving|refund|return(ed)?|"
    r"booking|reservation|itinerary|ticket|confirmation|confirmed|purchase|subscription (renew|confirm)|"
    r"statement (is )?(ready|available)|bill (is )?(ready|due)|autopay|transaction)\b")
PROMO = re.compile(
    r"(?i)(\d+\s?% off|\bsale\b|\bdeal(s)?\b|\bsave\b|\bsavings\b|\bfree shipping\b|limited time|"
    r"\bcoupon\b|\bpromo\b|last chance|ends (tonight|today|soon)|don'?t miss|\bnew arrivals?\b|"
    r"\bexclusive\b|\bshop now\b|\bbogo\b|\bgift\b|black friday|cyber monday|\bflash\b|\bclearance\b|"
    r"you('| ha)ve (won|been selected)|\bnewsletter\b|\bweekly\b|\bdigest\b|recommended for you|"
    r"trending|\bwebinar\b)")
CODE = re.compile(
    r"(?i)(verification code|one[- ]time (pass)?code|\botp\b|security code|your code|sign[- ]in code|"
    r"login code|\bpasscode\b|confirm your (email|account)|verify your (email|account)|\b\d{4,8}\b is your)")
SECURITY_ALERT = re.compile(r"(?i)(security alert|new sign[- ]in|new device|unusual (sign|activity)|"
                            r"password (was )?(changed|reset))")
JOB = re.compile(
    r"(?i)(application|applied|interview|recruit|candidate|position|job offer|offer letter|"
    r"hiring|assessment|onboarding|background check|thank you for (your )?(interest|applying))")
TAX_LEGAL = re.compile(r"(?i)(\bw-?2\b|\b1099\b|\b1098\b|tax (form|document|return)|\blease\b|"
                       r"\bcontract\b|policy document|insurance (card|policy)|\bdiploma\b|transcript)")
JOB_DOMAINS = {"greenhouse.io", "lever.co", "myworkday.com", "workday.com", "icims.com",
               "smartrecruiters.com", "ashbyhq.com", "jobvite.com", "indeed.com", "linkedin.com",
               "ziprecruiter.com", "joinhandshake.com", "handshake.com", "glassdoor.com"}
EMOJI = re.compile("[\U0001F300-\U0001FAFF☀-➿]")

# Conservative on purpose: when unsure, it goes to Review, never Delete.
CANDIDATE_AT = 0.90  # model this sure -> Claude needs auto_clean_min_certainty (90%) to delete
LEAN_AT = 0.60       # model leaning junk -> Claude needs to be 95%+ sure to delete
DELETE_AT = 0.95     # used when reporting how the model does on your past decisions
KEEP_AT = 0.15       # model this sure it's a keeper -> Keep
AUTO_LABEL = "Auto-cleaned"
SAMPLE_PER_SENDER = 5


# ---------------------------------------------------------------- sender features
# Note: email volume is deliberately NOT a feature. You reviewed the biggest senders
# first, so volume would teach the model "big = delete" and it would wave through
# small new mailing lists.
FEATURES = [
    "unread_ratio", "replied", "unsub_oneclick", "unsub_mailto", "unsub_link",
    "spammy", "freemail", "account_domain", "subj_transactional", "subj_promo", "subj_code",
    "subj_job", "subj_reply", "subj_emoji",
]
FEATURE_PLAIN = {
    "log_emails": "sends a lot of email", "unread_ratio": "you never open it",
    "replied": "you've emailed them", "unsub_oneclick": "one-click unsubscribe (bulk mailer)",
    "unsub_mailto": "email-to-unsubscribe (mailing list)", "unsub_link": "web unsubscribe link",
    "spammy": "sketchy-looking address", "freemail": "gmail/yahoo/etc. address",
    "account_domain": "company you log into", "subj_transactional": "receipt/order-type subjects",
    "subj_promo": "sale/promo-type subjects", "subj_code": "sign-in code subjects",
    "subj_job": "job-related subjects", "subj_reply": "Re:/Fwd: subjects",
    "subj_emoji": "emoji in subjects",
}


def _frac(subjects: list[str], rx) -> float:
    return sum(1 for s in subjects if rx.search(s)) / len(subjects) if subjects else 0.0


def sender_features(row: dict) -> list[float]:
    emails = max(to_int(row.get("Emails")), 1)
    unread = to_int(row.get("Unread"))
    subjects = [s.strip() for s in str(row.get("Sample subjects", "")).split(" | ") if s.strip()]
    unsub = (row.get("Unsubscribe") or "").lower()
    sender = (row.get("Sender") or "").lower()
    f = {
        "unread_ratio": min(unread / emails, 1.0),
        "replied": 1.0 if str(row.get("You replied?", "")).lower().startswith("y") else 0.0,
        "unsub_oneclick": 1.0 if unsub == "one-click" else 0.0,
        "unsub_mailto": 1.0 if unsub == "mailto" else 0.0,
        "unsub_link": 1.0 if unsub == "link" else 0.0,
        "spammy": 1.0 if row.get("Looks spammy?") else 0.0,
        "freemail": 1.0 if domain_of(sender) in FREEMAIL else 0.0,
        "account_domain": 1.0 if is_account_domain(base_domain(sender)) else 0.0,
        "subj_transactional": _frac(subjects, TRANSACTIONAL),
        "subj_promo": _frac(subjects, PROMO),
        "subj_code": _frac(subjects, CODE),
        "subj_job": _frac(subjects, JOB),
        "subj_reply": _frac(subjects, re.compile(r"(?i)^(re|fwd?):")),
        "subj_emoji": _frac(subjects, EMOJI),
    }
    return [f[k] for k in FEATURES]


def training_rows(review: list[dict], archive: list[dict],
                  important: set[str] | None = None) -> tuple[list[dict], list[int]]:
    """Delete examples: senders you deleted. Keep examples: senders you marked Keep,
    plus senders you've emailed and your Always Important senders (you've clearly
    decided those matter, even if you never picked "Keep" for them)."""
    important = important or set()
    rows, labels = [], []
    deleted = set()
    for r in archive:
        st = r.get("Status", "")
        if st.startswith("Done") or st.startswith("Covered"):
            rows.append(r)
            labels.append(1)
            deleted.add(r.get("Sender", "").lower())
    for r in review:
        a = r.get("Sender", "").lower()
        if not a or a in deleted:
            continue
        if r.get("Decision", "").startswith("Delete") and not r.get("Status", "").startswith("Skipped"):
            rows.append(r)          # marked Delete but not applied yet: still your decision
            labels.append(1)
            continue
        replied = str(r.get("You replied?", "")).lower().startswith("y")
        if (r.get("Decision") == "Keep" or r.get("Status") == "Kept" or replied
                or a in important or base_domain(a) in important):
            rows.append(r)
            labels.append(0)
    return rows, labels


def heuristic_p(x: list[float]) -> float:
    f = dict(zip(FEATURES, x))
    z = (-0.5 + 1.2 * f["unsub_oneclick"] + 0.8 * f["unsub_mailto"] + 0.6 * f["unsub_link"]
         + 1.5 * f["unread_ratio"] + 1.2 * f["subj_promo"] + 1.0 * f["spammy"]
         - 3.0 * f["replied"] - 1.5 * f["subj_transactional"] - 1.5 * f["subj_job"]
         - 1.0 * f["subj_reply"])
    return 1 / (1 + math.exp(-z))


class SenderModel:
    """Logistic regression on your past Delete/Keep decisions. Falls back to a
    hand-tuned score if there aren't enough decisions of each kind yet."""

    def __init__(self):
        self.clf = None
        self.report: dict = {"trained": False}

    def fit(self, rows: list[dict], labels: list[int]) -> None:
        n_del, n_keep = sum(labels), len(labels) - sum(labels)
        self.report.update({"deleted_examples": n_del, "kept_examples": n_keep})
        if n_del < 10 or n_keep < 10:
            self.report["note"] = ("Not enough decisions of both kinds to train yet "
                                   f"({n_del} deleted, {n_keep} kept; need 10+ each). Using built-in rules. "
                                   "Mark some mailing lists you actually want as Keep in Review Senders.")
            return
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import StratifiedKFold, cross_val_predict

        X = [sender_features(r) for r in rows]
        folds = max(2, min(5, n_del, n_keep))
        base = LogisticRegression(C=3.0, max_iter=2000)
        probs = cross_val_predict(base, X, labels, cv=StratifiedKFold(folds, shuffle=True, random_state=0),
                                  method="predict_proba")[:, 1]
        pred_del = [p >= DELETE_AT for p in probs]
        wrong_del = [rows[i]["Sender"] for i, (p, y) in enumerate(zip(pred_del, labels)) if p and y == 0]
        caught = sum(1 for p, y in zip(pred_del, labels) if p and y == 1)
        acc = sum(1 for p, y in zip(probs, labels) if (p >= 0.5) == bool(y)) / len(labels)
        self.clf = LogisticRegression(C=3.0, max_iter=2000).fit(X, labels)
        coefs = sorted(zip(FEATURES, self.clf.coef_[0]), key=lambda t: -abs(t[1]))
        self.report.update({
            "trained": True,
            "cv_accuracy": acc,
            "auto_delete_caught": caught,
            "kept_senders_it_would_delete": wrong_del,
            "top_signals": [(FEATURE_PLAIN[k], round(float(v), 2)) for k, v in coefs[:6]],
        })

    def p_delete(self, row: dict) -> float:
        x = sender_features(row)
        if self.clf is not None:
            return float(self.clf.predict_proba([x])[0][1])
        return heuristic_p(x)


# ---------------------------------------------------------------- per-message decision
def decide(m: dict, ctx: dict, verdict: dict | None = None) -> tuple[str, float, str]:
    """Returns (action, confidence, reason).

    action is delete / review / keep, or "candidate" when the rules and the model
    both think it's junk but nobody has read the body yet. A candidate only becomes
    a delete once Claude has read the body and is very sure (see verdict).
    """
    a = m["from_email"]
    subj = m["subject"]
    labels = set(m["labels"])
    age_days = (ctx["now_ms"] - m["date_ms"]) / DAY_MS
    promo = bool(PROMO.search(subj))
    listy = m["bulk"] or bool(labels & {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"})

    # hard rules: these win no matter what
    if a in ctx["protected"] or domain_of(a) in ctx["protected"] or base_domain(a) in ctx["protected"]:
        return "keep", 0.99, "on your Always Important list"
    nd = ctx.get("never_delete", set())
    if a in nd or domain_of(a) in nd or base_domain(a) in nd:
        return "keep", 0.99, "on your never-auto-delete list"
    if a in ctx["deleted"] or base_domain(a) in ctx["deleted_domains"]:
        return "delete", 0.99, "sender you already deleted (filter missed this one)"
    if a in ctx.get("pending_delete", set()):
        return "delete", 0.99, "sender you marked Delete (not applied yet)"
    if a in ctx["kept"]:
        return "keep", 0.98, "sender you marked Keep"
    if "STARRED" in labels:
        return "keep", 0.99, "you starred it"
    if m["thread_id"] in ctx["my_threads"]:
        return "keep", 0.98, "conversation you replied in"
    if a in ctx["replied"] or m.get("replied_before"):
        return "keep", 0.97, "someone you've emailed"
    if TAX_LEGAL.search(subj):
        return "keep", 0.97, "tax / legal / insurance document"

    # quiet sender: nothing from them (or their domain) in years -> delete on its own,
    # except receipts/orders, and anything with an attachment goes to Review.
    last = ctx.get("sender_last_ms", {}).get(a, m["date_ms"])
    personal = domain_of(a) in FREEMAIL and not listy
    if ctx.get("dormant_before_ms") and last < ctx["dormant_before_ms"] and not personal:
        years = (ctx["now_ms"] - last) / (365.25 * DAY_MS)
        if TRANSACTIONAL.search(subj) and not promo:
            return "keep", 0.95, "old receipt / order"
        if m.get("has_attachment"):
            return "review", 0.6, f"quiet sender ({years:.0f}+ yrs) but has an attachment"
        return "delete", 0.97, f"sender hasn't emailed you in {years:.0f}+ years"

    if JOB.search(subj) or base_domain(a) in JOB_DOMAINS:
        if promo and base_domain(a) in {"linkedin.com", "indeed.com", "ziprecruiter.com", "glassdoor.com"}:
            return "review", 0.6, "job-site mail that looks like a promo/digest"
        return "keep", 0.93, "job search"
    if CODE.search(subj):
        if age_days > 14:
            return "delete", 0.97, "old one-time sign-in code"
        return "keep", 0.9, "recent sign-in code"
    if SECURITY_ALERT.search(subj):
        return "keep", 0.92, "security alert"
    if TRANSACTIONAL.search(subj):
        if promo:
            return "review", 0.55, "looks like a receipt but reads like a promo"
        return "keep", 0.95, "receipt / order / delivery"
    if m.get("has_attachment"):
        return "keep", 0.9, "has an attachment"
    if not listy:
        if domain_of(a) in FREEMAIL:
            return "keep", 0.97, "personal email (not from a mailing list)"
        return "keep", 0.88, "not from a mailing list"
    if a in ctx.get("never_auto", set()):
        return "review", 0.5, "you recovered auto-deleted mail from this sender before"

    # mailing-list mail: the learned model + several independent junk signals
    p = ctx["sender_p"].get(a, 0.5)
    signals = []
    if labels & {"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL"}:
        signals.append("Promotions/Social tab")
    if promo:
        signals.append("promo subject")
    if ctx["sender_unread"].get(a, 0) >= 0.9:
        signals.append("never opened")
    if m.get("list_unsubscribe"):
        signals.append("list mail")

    if p <= KEEP_AT:
        return "keep", 1 - p, "mailing list like ones you've kept"
    if p < LEAN_AT or len(signals) < 2:
        return "review", max(p, 1 - p), f"mailing list, not sure enough ({p:.0%} delete)"
    if verdict is None:
        return "candidate", p, "looks like junk; body not checked yet"

    # The less sure the model is, the surer Claude has to be after reading it.
    need = ctx["min_certainty"] if p >= CANDIDATE_AT else max(ctx["min_certainty"], 95)
    certainty = int(verdict.get("junk_certainty") or 0)
    if verdict.get("tier") == "junk" and certainty >= need:
        return "delete", min(p, certainty / 100), (
            f"{' + '.join(signals[:3])} · Claude read it: {certainty}% junk")
    why = verdict.get("reason") or "wasn't sure"
    if why == "body not checked":
        return "review", max(p, 1 - p), "looks like junk, but Claude couldn't read the body"
    return "review", max(p, 1 - p), f"body check said keep an eye on it: {why}"[:160]


def trash_search_link(m: dict) -> str:
    q = f"in:anywhere rfc822msgid:{m['message_id']}" if m.get("message_id") \
        else f"in:trash from:{m['from_email']} subject:\"{m['subject'][:60]}\""
    return "https://mail.google.com/mail/u/0/#search/" + quote(q)


# ---------------------------------------------------------------- shared context
def sent_context(gmail: Gmail, query: str = "in:sent") -> tuple[set[str], set[str]]:
    ids = gmail.list_ids(query)
    msgs = gmail.get_batch(ids, headers=["To", "Cc"])
    people, threads = set(), set()
    for m in msgs.values():
        p = parse_message(m)
        people.update(addresses(p["to"]))
        people.update(addresses(p["cc"]))
        if m.get("threadId"):
            threads.add(m["threadId"])
    return people, threads


def build_context(sheet: S.Sheet, msgs: list[dict], replied: set[str], my_threads: set[str],
                  me: str, min_certainty: int = 90, dormant_years: float = 0,
                  never_delete: list[str] | tuple = ()) -> tuple[dict, SenderModel]:
    review = sheet.read(S.REVIEW)
    archive = sheet.read(S.ARCHIVE)
    protected = {r.get("Sender or domain", "").strip().lower() for r in sheet.read(S.IMPORTANT)
                 if r.get("Sender or domain", "").strip()}
    never_del = {d.strip().lower() for d in never_delete if d and d.strip()}
    never_auto = {r.get("Sender", "").lower() for r in sheet.read(S.LEARNED) if r.get("Direction") == "recovered"}

    rows, labels = training_rows(review, archive, protected)
    model = SenderModel()
    model.fit(rows, labels)

    done_deleted = {r["Sender"].lower() for r in archive
                    if r.get("Status", "").startswith(("Done", "Covered"))}
    pending_delete = {r["Sender"].lower() for r, y in zip(rows, labels) if y == 1} - done_deleted
    deleted_domains = {base_domain(r["Sender"]) for r in archive if r.get("Decision") == "Delete whole domain"
                       and r.get("Status", "").startswith("Done")}
    kept = {r["Sender"].lower() for r in review
            if r.get("Sender") and (r.get("Decision") == "Keep" or r.get("Status") == "Kept")}

    # sender rows: what we see now, enriched with the original scan when we have it
    known = {r.get("Sender", "").lower(): r for r in review}
    sender_rows = {}
    for r in aggregate(msgs, replied, me=me):
        k = known.get(r["Sender"])
        sender_rows[r["Sender"]] = {**r, **{c: k[c] for c in ("Emails", "Unread", "Sample subjects", "Unsubscribe")
                                            if k and k.get(c)}} if k else r
    sender_p = {a: model.p_delete(r) for a, r in sender_rows.items()}
    sender_unread = {a: to_int(r.get("Unread")) / max(to_int(r.get("Emails")), 1) for a, r in sender_rows.items()}
    # when did each sender (or, for company domains, anyone at that domain) last email you?
    by_addr: dict[str, int] = {}
    by_domain: dict[str, int] = {}
    for m in msgs:
        a = m["from_email"]
        by_addr[a] = max(by_addr.get(a, 0), m["date_ms"])
        d = base_domain(a)
        by_domain[d] = max(by_domain.get(d, 0), m["date_ms"])
    sender_last = {a: (t if domain_of(a) in FREEMAIL else max(t, by_domain.get(base_domain(a), 0)))
                   for a, t in by_addr.items()}
    now_ms = int(time.time() * 1000)
    ctx = {"now_ms": now_ms, "sender_last_ms": sender_last,
           "dormant_before_ms": now_ms - int(dormant_years * 365.25 * DAY_MS) if dormant_years else 0,
           "deleted": done_deleted, "pending_delete": pending_delete, "deleted_domains": deleted_domains,
           "kept": kept, "protected": protected, "replied": replied, "my_threads": my_threads,
           "sender_p": sender_p, "sender_unread": sender_unread, "never_auto": never_auto,
           "never_delete": never_del,
           "min_certainty": min_certainty}
    return ctx, model


def body_check(gmail: Gmail, msgs: list[dict], cfg) -> list[dict]:
    """Read the body of each message and ask Claude for a tier + junk certainty."""
    from zoneinfo import ZoneInfo

    from .classify import classify
    from .gmail import extract_body

    if not msgs:
        return []
    if not cfg.anthropic_api_key:
        log.warning("No ANTHROPIC_API_KEY; skipping body checks (candidates go to Review)")
        return [{"tier": "worth_a_look", "junk_certainty": 0, "reason": "body not checked"} for _ in msgs]
    full = gmail.get_batch([m["id"] for m in msgs], fmt="full")
    for m in msgs:
        m["body"] = extract_body(full.get(m["id"], {}), cfg.body_chars) or m.get("snippet", "")
    return classify(msgs, cfg, ZoneInfo(cfg.timezone), learned=[], important=set())


def resolve_candidates(gmail: Gmail, cand: list[dict], ctx: dict, cfg, max_checks: int) -> dict[str, tuple]:
    """Body-check up to SAMPLE_PER_SENDER newest candidates per sender.

    A sender's unchecked candidates are only deleted if every checked one came back
    as confident junk; otherwise they go to Review.
    """
    by_sender: dict[str, list[dict]] = defaultdict(list)
    for m in sorted(cand, key=lambda m: -m["date_ms"]):
        by_sender[m["from_email"]].append(m)
    sample: list[dict] = []
    for a, ms in sorted(by_sender.items(), key=lambda kv: -len(kv[1])):
        take = ms[:SAMPLE_PER_SENDER]
        if len(sample) + len(take) > max_checks:
            break
        sample.extend(take)
    log.info("Body-checking %d emails from %d senders", len(sample), len({m['from_email'] for m in sample}))
    verdicts = body_check(gmail, sample, cfg)

    out: dict[str, tuple] = {}
    sender_ok: dict[str, bool] = {}
    for m, v in zip(sample, verdicts):
        res = decide(m, ctx, v)
        out[m["id"]] = res
        sender_ok[m["from_email"]] = sender_ok.get(m["from_email"], True) and res[0] == "delete"
    for a, ms in by_sender.items():
        checked = [m for m in ms if m["id"] in out]
        for m in ms:
            if m["id"] in out:
                continue
            if checked and sender_ok.get(a):
                conf = min(out[c["id"]][1] for c in checked)
                out[m["id"]] = ("delete", conf, f"same as the {len(checked)} emails from this sender Claude read (all junk)")
            elif checked:
                out[m["id"]] = ("review", 0.5, "some emails from this sender looked worth keeping")
            else:
                out[m["id"]] = ("review", 0.5, "looks like junk but body not checked (over the check limit)")
    return out


def auto_clean(gmail: Gmail, sheet: S.Sheet, deletes: list[dict], source: str) -> int:
    """Trash messages, tag them Auto-cleaned, and queue them for the digest's list."""
    if not deletes:
        return 0
    lid = gmail.label_id(AUTO_LABEL)
    ids = [m["id"] for m in deletes]
    gmail.modify(ids, add=[lid])
    gmail.trash(ids)
    pending = sheet.get_state("auto_cleaned_since_digest", {"count": 0, "items": []}) or {"count": 0, "items": []}
    items = pending.get("items", [])
    for m in deletes:
        if len(items) >= 60:
            break
        items.append({"sender": m.get("from_name") or m["from_email"], "email": m["from_email"],
                      "subject": m["subject"][:120], "why": m.get("why", "")[:120],
                      "link": trash_search_link(m), "source": source})
    sheet.set_state(auto_cleaned_since_digest={"count": pending.get("count", 0) + len(deletes), "items": items})
    return len(deletes)


def detect_recoveries(gmail: Gmail, sheet: S.Sheet) -> list[str]:
    """Auto-cleaned mail that's no longer in Trash means you pulled it back out.
    Remember those senders so they're never auto-deleted again."""
    ids = gmail.list_ids(f"label:{AUTO_LABEL}")  # list() skips Trash, so these were recovered
    if not ids:
        return []
    metas = gmail.get_batch(ids, headers=["From", "Subject"])
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    senders, rows = [], []
    for m in metas.values():
        p = parse_message(m)
        if p["from_email"] and p["from_email"] not in senders:
            senders.append(p["from_email"])
            rows.append({"Sender": p["from_email"], "Direction": "recovered",
                         "Example subject": p["subject"][:200], "Date": today})
    gmail.modify(ids, remove=[gmail.label_id(AUTO_LABEL)])
    sheet.append(S.LEARNED, rows, raw=True)
    log.info("You recovered mail from %d sender(s); they won't be auto-deleted again", len(senders))
    return senders


# ---------------------------------------------------------------- the sweep
def run_sweep(gmail: Gmail, sheet: S.Sheet, cfg=None, query: str = "-in:sent -in:chats -in:drafts",
              limit: int | None = None, clean: bool = False, max_checks: int | None = None) -> dict:
    from .config import load_config

    cfg = cfg or load_config()
    max_checks = max_checks if max_checks is not None else cfg.sweep_max_body_checks
    t0 = time.time()
    sheet.ensure_tabs()
    me = gmail.me()

    if clean:
        detect_recoveries(gmail, sheet)
    log.info("Reading sent mail for context...")
    replied, my_threads = sent_context(gmail)
    log.info("Listing remaining mail...")
    ids = gmail.list_ids(query, limit=limit)
    attach = set(gmail.list_ids(f"({query}) has:attachment"))
    log.info("Reading headers for %s messages", f"{len(ids):,}")
    last = {"t": 0}

    def prog(done, total):
        if done - last["t"] >= 2000 or done == total:
            last["t"] = done
            log.info("headers read: %s / %s", f"{done:,}", f"{total:,}")

    raw = gmail.get_batch(ids, progress=prog)
    msgs = []
    for mid, r in raw.items():
        m = parse_message(r)
        if not m["from_email"] or m["from_email"] == me:
            continue
        m["has_attachment"] = mid in attach
        m["message_id"] = next((h["value"] for h in r.get("payload", {}).get("headers", [])
                                if h.get("name", "").lower() == "message-id"), "").strip("<>")
        msgs.append(m)

    ctx, model = build_context(sheet, msgs, replied, my_threads, me, cfg.auto_clean_min_certainty,
                               cfg.dormant_years, cfg.never_auto_delete)
    log.info("Model: %s", model.report)

    decisions = {m["id"]: decide(m, ctx) for m in msgs}
    cand = [m for m in msgs if decisions[m["id"]][0] == "candidate"]
    decisions.update(resolve_candidates(gmail, cand, ctx, cfg, max_checks))

    groups: dict[tuple[str, str], dict] = {}
    action_count = Counter()
    reason_count: dict[str, Counter] = defaultdict(Counter)
    sender_actions: dict[str, set] = defaultdict(set)
    deletes = []
    for m in msgs:
        action, conf, why = decisions[m["id"]]
        m["why"] = why
        if action == "delete":
            deletes.append(m)
        action_count[action] += 1
        key = ("junk, Claude read it and agreed" if "Claude read it" in why
               else "sender quiet for years" if why.startswith("sender hasn't emailed")
               else "mailing list, model not leaning junk" if why.startswith("mailing list, not sure enough")
               else "Claude read it and wasn't sure enough" if why.startswith("body check said")
               else why)
        reason_count[action][key] += 1
        sender_actions[m["from_email"]].add(action)
        g = groups.setdefault((m["from_email"], action), {
            "Sender": m["from_email"], "Name": m["from_name"], "Action": action.title(),
            "Emails": 0, "_conf": [], "_why": Counter(), "_subjects": [], "Unread": 0,
            "Model says delete": f"{ctx['sender_p'].get(m['from_email'], 0):.0%}"})
        g["Emails"] += 1
        g["Unread"] += 1 if m["unread"] else 0
        g["_conf"].append(conf)
        g["_why"][why] += 1
        if len(g["_subjects"]) < 3 and m["subject"] not in g["_subjects"]:
            g["_subjects"].append(m["subject"])

    mixed = {a for a, acts in sender_actions.items() if len(acts) > 1}
    out = []
    for g in groups.values():
        confs = g.pop("_conf")
        g["Confidence"] = f"{min(confs):.0%}–{max(confs):.0%}" if len(confs) > 1 and min(confs) != max(confs) \
            else f"{confs[0]:.0%}"
        whys = g.pop("_why")
        g["Why"] = "; ".join(f"{w} ({n})" if len(whys) > 1 else w for w, n in whys.most_common(3))
        g["Sample subjects"] = " | ".join(s[:90] for s in g.pop("_subjects"))
        g["Mixed sender?"] = "Yes" if g["Sender"] in mixed else ""
        out.append(g)
    order = {"Delete": 0, "Review": 1, "Keep": 2}
    out.sort(key=lambda g: (order[g["Action"]], -g["Emails"]))
    sheet.replace_all(S.SWEEP, out)

    cleaned = auto_clean(gmail, sheet, deletes, "sweep") if clean else 0

    total = len(msgs)
    risks = {
        "Senders that send both keepers and junk (e.g. Amazon: orders + promos)": len(mixed),
        "Receipt-looking emails that also read like promos": reason_count["review"].get(
            "looks like a receipt but reads like a promo", 0),
        "Gmail/Yahoo addresses using mailing-list software (personal-looking bulk mail)":
            sum(1 for m in msgs if domain_of(m["from_email"]) in FREEMAIL and m["bulk"]),
        "Deleted only because the sender went quiet for years":
            reason_count["delete"].get("sender quiet for years", 0),
        "Emails from senders you already deleted (filter leaks)": reason_count["delete"].get(
            "sender you already deleted (filter missed this one)", 0),
        "Looked like junk, but Claude's read of the body said keep an eye on it":
            reason_count["review"].get("Claude read it and wasn't sure enough", 0),
        "Looked like junk, but over the body-check limit (left in Review)":
            reason_count["review"].get("looks like junk but body not checked (over the check limit)", 0),
    }
    summary = [
        ("Run at", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")),
        ("Mode", f"CLEANED: {cleaned:,} emails moved to Trash (label: {AUTO_LABEL})" if clean
         else "Preview only, nothing changed"),
        ("Emails looked at", total),
        ("Would delete", f"{action_count['delete']:,} ({action_count['delete'] / max(total, 1):.0%})"),
        ("Would keep", f"{action_count['keep']:,} ({action_count['keep'] / max(total, 1):.0%})"),
        ("Needs review", f"{action_count['review']:,} ({action_count['review'] / max(total, 1):.0%}) "
                         f"from {sum(1 for acts in sender_actions.values() if 'review' in acts):,} senders"),
        ("", ""),
        ("MODEL (learned from your decisions)", ""),
    ]
    rep = model.report
    if rep.get("trained"):
        summary += [
            ("Trained on", f"{rep['deleted_examples']} deleted senders, {rep['kept_examples']} kept senders"),
            ("Accuracy on your past decisions (cross-validated)", f"{rep['cv_accuracy']:.0%}"),
            (f"Senders you KEPT that it would have flagged at {DELETE_AT:.0%}",
             f"{len(rep['kept_senders_it_would_delete'])}: " + ", ".join(rep["kept_senders_it_would_delete"][:15])),
            (f"Senders you deleted that it would also flag at {DELETE_AT:.0%}",
             f"{rep['auto_delete_caught']} of {rep['deleted_examples']}"),
            ("Strongest signals (+ = toward delete)", "; ".join(f"{k} ({v:+})" for k, v in rep["top_signals"])),
        ]
    else:
        summary += [("Note", rep.get("note", ""))]
    summary += [("", ""), ("TOP REASONS", "")]
    for action in ("delete", "review", "keep"):
        for why, n in reason_count[action].most_common(6):
            summary.append((f"{action.title()}: {why}", f"{n:,}"))
    summary += [("", ""), ("THINGS THAT COULD CAUSE MISTAKES", "")]
    summary += [(k, f"{v:,}") for k, v in risks.items()]
    summary.append(("Took", f"{(time.time() - t0) / 60:.0f} min"))
    sheet.replace_all(S.SWEEP_SUMMARY, [{"Item": k, "Value": v} for k, v in summary], raw=True)
    log.info("Sweep done: %s%s", dict(action_count), f"; cleaned {cleaned}" if clean else "")
    return {"actions": dict(action_count), "model": rep, "risks": risks, "cleaned": cleaned}
