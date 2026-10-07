"""Pure helpers: parse senders, group messages by sender, spot likely spam."""
from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import getaddresses, parseaddr

FREEMAIL = {
    "gmail.com", "googlemail.com", "yahoo.com", "ymail.com", "outlook.com", "hotmail.com",
    "live.com", "msn.com", "icloud.com", "me.com", "mac.com", "aol.com", "proton.me",
    "protonmail.com", "gmx.com", "zoho.com", "mail.com", "yandex.com", "fastmail.com",
}
MULTI_PART_TLDS = {"co.uk", "com.au", "co.nz", "co.jp", "com.br", "co.in", "com.mx", "co.za", "org.uk"}
# Companies you log into. "Delete whole domain" refuses these, because their
# sign-in codes, password resets and receipts come from the same domain as promos.
ACCOUNT_DOMAINS = {
    # big tech / gaming
    "microsoft.com", "xbox.com", "live.com", "office.com", "google.com", "youtube.com",
    "apple.com", "amazon.com", "amazonses.com", "meta.com", "facebookmail.com", "instagram.com",
    "x.com", "twitter.com", "discord.com", "steampowered.com", "epicgames.com", "playstation.com",
    "sony.com", "nintendo.com", "riotgames.com", "ea.com", "twitch.tv", "spotify.com", "netflix.com",
    "github.com", "dropbox.com", "adobe.com", "anthropic.com", "openai.com",
    # money
    "paypal.com", "venmo.com", "cash.app", "squareup.com", "zellepay.com", "chase.com",
    "bankofamerica.com", "wellsfargo.com", "capitalone.com", "citi.com", "discover.com",
    "americanexpress.com", "aexp.com", "usbank.com", "pnc.com", "ally.com", "sofi.com",
    "navyfederal.org", "usaa.com", "schwab.com", "fidelity.com", "vanguard.com", "robinhood.com",
    "coinbase.com", "creditkarma.com", "experian.com", "equifax.com", "transunion.com",
    "turbotax.com", "intuit.com",
    # phone / utilities / travel
    "att.com", "verizon.com", "t-mobile.com", "xfinity.com", "comcast.net", "spectrum.com",
    "uber.com", "lyft.com", "doordash.com", "airbnb.com",
    # job search (application updates come from these)
    "linkedin.com", "indeed.com", "ziprecruiter.com", "glassdoor.com", "greenhouse.io",
    "lever.co", "myworkday.com", "workday.com", "icims.com", "smartrecruiters.com",
    "ashbyhq.com", "jobvite.com", "handshake.com", "joinhandshake.com",
}
ACCOUNT_TLDS = ("gov", "edu", "mil")
SECURITY_SUBJECT = re.compile(
    r"(?i)verif|security (code|alert)|sign[- ]?in|log[- ]?in|password|one[- ]time|\bcode\b|"
    r"\b2fa\b|two[- ]factor|confirm your|new device|account (alert|activity|locked)|"
    r"receipt|order (confirm|#)|invoice|payment|statement|application|interview")


def is_account_domain(domain: str) -> bool:
    d = domain.lower()
    return d in ACCOUNT_DOMAINS or d.rsplit(".", 1)[-1] in ACCOUNT_TLDS


SHADY_TLDS = {"xyz", "top", "click", "icu", "buzz", "rest", "cfd", "sbs", "monster", "cyou", "lol", "shop", "fun"}


def parse_from(value: str) -> tuple[str, str]:
    name, addr = parseaddr(value or "")
    return name.strip().strip('"'), addr.strip().lower()


def addresses(value: str) -> list[str]:
    return [a.lower() for _, a in getaddresses([value or ""]) if a]


def domain_of(addr: str) -> str:
    """Full domain after @ (e.g. mail.quora.com)."""
    return addr.rsplit("@", 1)[-1].lower() if "@" in addr else ""


def base_domain(addr_or_domain: str) -> str:
    """Registrable domain, roughly: mail.quora.com -> quora.com, x.co.uk -> y.x.co.uk kept to 3."""
    d = domain_of(addr_or_domain) if "@" in addr_or_domain else addr_or_domain.lower()
    parts = d.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in MULTI_PART_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def unsubscribe_kind(list_unsub: str, list_unsub_post: str) -> str:
    from .unsubscribe import plan
    return plan(list_unsub, list_unsub_post)["method"]


def looks_spammy(addr: str, name: str, has_unsub: bool, replied: bool) -> bool:
    """Rough hint only. The user makes the call."""
    if replied:
        return False
    dom = domain_of(addr)
    local = addr.split("@", 1)[0]
    tld = dom.rsplit(".", 1)[-1]
    signals = 0
    if tld in SHADY_TLDS:
        signals += 2
    if re.search(r"[a-z]{2,}\d{4,}|[0-9a-f]{10,}", local):
        signals += 1
    if sum(c.isdigit() for c in dom) >= 3:
        signals += 1
    if name and "@" in name and parse_from(name)[1] and base_domain(parse_from(name)[1]) != base_domain(dom):
        signals += 2  # display name pretends to be another address
    if not has_unsub:
        signals += 1
    if dom in FREEMAIL and not has_unsub and re.search(r"(?i)winner|prize|urgent|bitcoin|crypto|refund|invoice", name or ""):
        signals += 1
    return signals >= 2


def iso_day(ms: int | str) -> str:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")
    except (TypeError, ValueError):
        return ""


def parse_message(msg: dict) -> dict:
    """Flatten a Gmail metadata message into the fields we care about."""
    hdrs = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
    name, addr = parse_from(hdrs.get("from", ""))
    return {
        "id": msg.get("id"),
        "thread_id": msg.get("threadId"),
        "labels": msg.get("labelIds", []) or [],
        "unread": "UNREAD" in (msg.get("labelIds") or []),
        "date_ms": int(msg.get("internalDate", 0) or 0),
        "from_name": name,
        "from_email": addr,
        "to": hdrs.get("to", ""),
        "cc": hdrs.get("cc", ""),
        "subject": hdrs.get("subject", "") or "(no subject)",
        "snippet": msg.get("snippet", ""),
        "list_unsubscribe": hdrs.get("list-unsubscribe", ""),
        "list_unsubscribe_post": hdrs.get("list-unsubscribe-post", ""),
        "bulk": bool(hdrs.get("list-unsubscribe") or hdrs.get("precedence", "").lower() in ("bulk", "list", "junk")
                     or hdrs.get("auto-submitted", "no").lower() not in ("", "no")),
    }


def aggregate(messages: list[dict], replied: set[str], me: str = "") -> list[dict]:
    """Group parsed messages into one review row per sender address."""
    by_sender: dict[str, list[dict]] = defaultdict(list)
    for m in messages:
        if not m["from_email"] or m["from_email"] == me:
            continue
        by_sender[m["from_email"]].append(m)

    domain_totals: dict[str, int] = defaultdict(int)
    for addr, msgs in by_sender.items():
        domain_totals[base_domain(addr)] += len(msgs)

    rows = []
    for addr, msgs in by_sender.items():
        msgs.sort(key=lambda m: m["date_ms"], reverse=True)
        newest = msgs[0]
        subjects: list[str] = []
        for m in msgs:
            s = m["subject"].strip()
            if s and s not in subjects:
                subjects.append(s)
            if len(subjects) == 3:
                break
        names = [m["from_name"] for m in msgs if m["from_name"]]
        name = max(set(names), key=names.count) if names else ""
        with_unsub = next((m for m in msgs if m["list_unsubscribe"]), newest)
        unsub = unsubscribe_kind(with_unsub["list_unsubscribe"], with_unsub["list_unsubscribe_post"])
        was_replied = addr in replied
        rows.append({
            "Decision": "",
            "Sender": addr,
            "Name": name,
            "Domain": base_domain(addr),
            "Emails": len(msgs),
            "Unread": sum(1 for m in msgs if m["unread"]),
            "Domain total": domain_totals[base_domain(addr)],
            "Last email": iso_day(newest["date_ms"]),
            "First email": iso_day(msgs[-1]["date_ms"]),
            "Sample subjects": " | ".join(s[:90] for s in subjects),
            "You replied?": "Yes" if was_replied else "",
            "Unsubscribe": unsub,
            "Looks spammy?": "Maybe" if looks_spammy(addr, name, unsub != "none", was_replied) else "",
            "Source": "backlog",
            "Status": "",
            "Processed at": "",
        })
    rows.sort(key=lambda r: r["Emails"], reverse=True)
    return rows


def to_int(v) -> int:
    try:
        return int(str(v).replace(",", "").strip() or 0)
    except ValueError:
        return 0


def merge_review(existing: list[dict], fresh: list[dict]) -> list[dict]:
    """Keep the user's decisions and statuses; refresh counts; order for review.

    Order: undecided/Later first (biggest senders on top), then Keep, then processed.
    """
    old = {r.get("Sender", "").lower(): r for r in existing if r.get("Sender")}
    merged: dict[str, dict] = {}
    for r in fresh:
        o = old.get(r["Sender"])
        if o:
            for k in ("Decision", "Status", "Processed at", "Source"):
                if o.get(k):
                    r[k] = o[k]
        merged[r["Sender"]] = r
    for key, o in old.items():  # senders no longer in mailbox (e.g. already deleted)
        if key not in merged:
            row = {k: v for k, v in o.items() if k != "_row"}
            row["Emails"] = to_int(row.get("Emails"))
            merged[key] = row

    def rank(r):
        dec = r.get("Decision", "")
        if r.get("Status"):
            group = 2
        elif dec == "Keep":
            group = 1
        else:
            group = 0
        return (group, -to_int(r.get("Emails")))

    return sorted(merged.values(), key=rank)
