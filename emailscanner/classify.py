"""Rate emails with Claude: tier, reason, summary, plus job / event / bill details."""
from __future__ import annotations

import json
import logging
from datetime import datetime

log = logging.getLogger(__name__)

TIERS = ["priority", "worth_a_look", "low", "junk"]
TIER_RANK = {t: i for i, t in enumerate(TIERS)}

TOOL = {
    "name": "record_ratings",
    "description": "Record a rating for every email in the batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "ratings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "tier": {"type": "string", "enum": TIERS},
                        "reason": {"type": "string", "description": "Why this tier, max ~12 words."},
                        "summary": {"type": "string", "description": "One plain sentence: what it says / what's needed."},
                        "action_needed": {"type": "boolean"},
                        "junk_certainty": {
                            "type": "integer", "minimum": 0, "maximum": 100,
                            "description": "How sure you are this is pure junk that is safe to delete "
                                           "without the user ever seeing it. 0 unless tier is junk."},
                        "category": {"type": "string", "enum": [
                            "job", "bill", "receipt", "event", "personal", "security",
                            "delivery", "newsletter", "promo", "social", "notification", "other"]},
                        "job": {
                            "type": ["object", "null"],
                            "description": "Only for emails about the user's own job search.",
                            "properties": {
                                "company": {"type": "string"},
                                "role": {"type": "string"},
                                "status": {"type": "string", "enum": [
                                    "applied", "recruiter_outreach", "interview", "assessment",
                                    "rejected", "offer", "follow_up", "other"]},
                            },
                        },
                        "event": {
                            "type": ["object", "null"],
                            "description": "A specific dated thing the user may want on a calendar "
                                           "(interview, appointment, deadline). Not marketing 'events'.",
                            "properties": {
                                "title": {"type": "string"},
                                "start": {"type": "string", "description": "ISO 8601 local time, or YYYY-MM-DD if all-day"},
                                "end": {"type": ["string", "null"]},
                                "all_day": {"type": "boolean"},
                                "location": {"type": ["string", "null"]},
                            },
                        },
                        "bill": {
                            "type": ["object", "null"],
                            "properties": {
                                "vendor": {"type": "string"},
                                "kind": {"type": "string", "enum": ["bill", "receipt"]},
                                "amount": {"type": ["string", "null"]},
                                "due": {"type": ["string", "null"], "description": "YYYY-MM-DD"},
                            },
                        },
                    },
                    "required": ["index", "tier", "reason", "summary", "category"],
                },
            }
        },
        "required": ["ratings"],
    },
}

SYSTEM = """You sort a person's incoming email so they only have to read what matters.

About them:
{about_me}

Tiers:
- priority: needs their attention or action soon. Real people writing to them, recruiters,
  interviews, job application updates, bills due / payment problems, security alerts,
  appointments, anything time-sensitive.
- worth_a_look: useful but not urgent. Includes anything that is even somewhat important or
  that you're unsure about.
- low: automated but legit and mildly relevant (receipts, shipping updates, account notices,
  newsletters they might read).
- junk: marketing, promos, social media notifications, digests, cold sales, spam. Only use
  junk when there is clearly nothing important in it. When in doubt, pick low or higher.

junk_certainty: only for junk. 95-100 means pure marketing / newsletter / social notification
with nothing the person could need later. Use 80 or lower if there is ANY chance it matters:
anything about an account, order, delivery, payment, refund, subscription change, reservation,
appointment, policy/terms change, job, school, health, legal, or a real person writing.
Emails with a high junk_certainty may be moved to Trash automatically, so be conservative.
Today is {today} ({tz}). Resolve relative dates ("this Thursday") against each email's date.
Fill job / event / bill only when they clearly apply, otherwise null.
Rate every email in the batch, using its index."""


def _learned_text(learned: list[dict], important: set[str]) -> str:
    lines = []
    if important:
        lines.append("Always-important senders/domains (the user set these): " + ", ".join(sorted(important)[:80]))
    ups = sorted({l["Sender"] for l in learned if l.get("Direction") == "up"})
    downs = sorted({l["Sender"] for l in learned if l.get("Direction") == "down"} - set(ups))
    if ups:
        lines.append("The user marked mail from these senders as more important than you rated it: " + ", ".join(ups[:60]))
    if downs:
        lines.append("The user marked mail from these senders as less important than you rated it: " + ", ".join(downs[:60]))
    return "\n".join(lines)


def format_email(i: int, e: dict, tz) -> str:
    when = datetime.fromtimestamp(e["date_ms"] / 1000, tz).strftime("%a %Y-%m-%d %H:%M")
    flags = []
    if e.get("bulk"):
        flags.append("bulk/automated headers")
    if e.get("replied_before"):
        flags.append("user has emailed this sender before")
    return (f"<email index=\"{i}\">\nFrom: {e['from_name']} <{e['from_email']}>\nDate: {when}\n"
            f"Subject: {e['subject']}\nFlags: {', '.join(flags) or 'none'}\n"
            f"Body:\n{e.get('body') or e.get('snippet', '')}\n</email>")


def fallback(e: dict, why: str) -> dict:
    return {"tier": "worth_a_look", "reason": why, "summary": e.get("snippet", "")[:200],
            "category": "other", "action_needed": False, "job": None, "event": None, "bill": None,
            "junk_certainty": 0}


def classify(emails: list[dict], cfg, tz, learned: list[dict], important: set[str],
             client=None, batch_size: int = 10) -> list[dict]:
    """Returns one rating dict per email (same order)."""
    if not emails:
        return []
    if client is None:
        import anthropic
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key or None)

    system = SYSTEM.format(about_me=cfg.about_me.strip(), today=datetime.now(tz).strftime("%A %Y-%m-%d"),
                           tz=cfg.timezone)
    extra = _learned_text(learned, important)
    if extra:
        system += "\n\n" + extra

    out: list[dict | None] = [None] * len(emails)
    for start in range(0, len(emails), batch_size):
        batch = emails[start:start + batch_size]
        prompt = "\n\n".join(format_email(i, e, tz) for i, e in enumerate(batch))
        try:
            resp = client.messages.create(
                model=cfg.model, max_tokens=4096, system=system,
                tools=[TOOL], tool_choice={"type": "tool", "name": "record_ratings"},
                messages=[{"role": "user", "content": prompt}],
            )
            ratings = []
            for block in resp.content:
                if getattr(block, "type", "") == "tool_use":
                    data = block.input if isinstance(block.input, dict) else json.loads(block.input)
                    ratings = data.get("ratings", [])
            for r in ratings:
                i = r.get("index")
                if isinstance(i, int) and 0 <= i < len(batch) and r.get("tier") in TIER_RANK:
                    out[start + i] = r
        except Exception as ex:  # noqa: BLE001 - never let one bad batch kill the digest
            log.warning("Rating batch failed: %s", ex)
        for i, e in enumerate(batch):
            if out[start + i] is None:
                out[start + i] = fallback(e, "Couldn't rate this one automatically")

    # Hard rules that override the model
    for e, r in zip(emails, out):
        addr = e["from_email"]
        dom = addr.rsplit("@", 1)[-1]
        if addr in important or dom in important or ".".join(dom.split(".")[-2:]) in important:
            r["tier"] = "priority"
            r["reason"] = "On your Always Important list. " + r.get("reason", "")
        elif e.get("replied_before") and r["tier"] == "junk":
            r["tier"] = "low"  # someone you've written to is never junk
        if r.get("job") and r["tier"] in ("low", "junk"):
            r["tier"] = "worth_a_look"
    for r in out:
        if r["tier"] != "junk" or r.get("job") or r.get("bill") or r.get("event"):
            r["junk_certainty"] = 0
    return out  # type: ignore[return-value]
