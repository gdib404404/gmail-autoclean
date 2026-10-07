"""Build the digest email (HTML + plain text)."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from html import escape
from urllib.parse import urlencode

TIER_TITLES = {
    "priority": "Needs you",
    "worth_a_look": "Worth a look",
    "low": "Low",
    "junk": "Probably junk",
}
JOB_STATUS = {
    "applied": "Application received", "recruiter_outreach": "Recruiter reached out",
    "interview": "Interview", "assessment": "Assessment", "rejected": "Rejected",
    "offer": "Offer", "follow_up": "Follow-up", "other": "Update",
}

C = {"text": "#1f2328", "muted": "#656d76", "line": "#e4e7eb", "accent": "#b42318",
     "blue": "#0b57d0", "bg": "#f6f7f9", "chip": "#eef1f4"}


def thread_link(thread_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}"


def calendar_link(event: dict, tz_name: str, details: str = "") -> str:
    """Google Calendar 'add event' link, prefilled. Returns '' if the date can't be read."""
    start_raw = (event or {}).get("start") or ""
    if not start_raw:
        return ""
    params = {"action": "TEMPLATE", "text": event.get("title") or "Event"}
    try:
        if event.get("all_day") or len(start_raw) == 10:
            d0 = date.fromisoformat(start_raw[:10])
            end_raw = event.get("end") or ""
            d1 = date.fromisoformat(end_raw[:10]) + timedelta(days=1) if len(end_raw) >= 10 else d0 + timedelta(days=1)
            params["dates"] = f"{d0:%Y%m%d}/{d1:%Y%m%d}"
        else:
            s = datetime.fromisoformat(start_raw.replace("Z", "+00:00"))
            e_raw = event.get("end")
            e = datetime.fromisoformat(e_raw.replace("Z", "+00:00")) if e_raw and len(e_raw) > 10 else s + timedelta(hours=1)
            if s.tzinfo:
                s, e = s.astimezone(timezone.utc), e.astimezone(timezone.utc)
                params["dates"] = f"{s:%Y%m%dT%H%M%SZ}/{e:%Y%m%dT%H%M%SZ}"
            else:
                params["dates"] = f"{s:%Y%m%dT%H%M%S}/{e:%Y%m%dT%H%M%S}"
                params["ctz"] = tz_name
    except ValueError:
        return ""
    if event.get("location"):
        params["location"] = event["location"]
    if details:
        params["details"] = details
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def human_when(event: dict) -> str:
    raw = (event or {}).get("start") or ""
    try:
        if event.get("all_day") or len(raw) == 10:
            d = date.fromisoformat(raw[:10])
            return f"{d:%a %b} {d.day}"
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return f"{dt:%a %b} {dt.day}, {dt.hour % 12 or 12}:{dt:%M} {'AM' if dt.hour < 12 else 'PM'}"
    except ValueError:
        return raw


def _sender(it: dict) -> str:
    return it.get("from_name") or it.get("from_email", "")


def _section(title: str, body: str, count: int | None = None) -> str:
    badge = f' <span style="color:{C["muted"]};font-weight:400">({count})</span>' if count else ""
    return (f'<h2 style="font-size:15px;margin:26px 0 8px;padding-bottom:6px;'
            f'border-bottom:1px solid {C["line"]};color:{C["text"]}">{escape(title)}{badge}</h2>{body}')


def _item(it: dict, show_reason: bool = True) -> str:
    link = thread_link(it["thread_id"])
    reason = (f'<div style="color:{C["muted"]};font-size:12px;margin-top:2px">Why: {escape(it.get("reason", ""))}</div>'
              if show_reason and it.get("reason") else "")
    action = (f'<span style="background:{C["accent"]};color:#fff;font-size:11px;padding:1px 6px;'
              f'border-radius:3px;margin-left:6px">action</span>') if it.get("action_needed") else ""
    return (f'<div style="margin:0 0 14px">'
            f'<div style="font-size:14px"><b>{escape(_sender(it))}</b>{action}</div>'
            f'<a href="{link}" style="color:{C["blue"]};font-size:14px;text-decoration:none">{escape(it["subject"])}</a>'
            f'<div style="font-size:14px;margin-top:2px">{escape(it.get("summary", ""))}</div>{reason}</div>')


def _compact(items: list[dict], limit: int = 40) -> str:
    rows = []
    for it in items[:limit]:
        rows.append(f'<li style="margin:0 0 4px"><a href="{thread_link(it["thread_id"])}" '
                    f'style="color:{C["text"]};text-decoration:none"><b>{escape(_sender(it))}</b> '
                    f'<span style="color:{C["muted"]}">· {escape(it["subject"][:90])}</span></a></li>')
    more = f'<li style="color:{C["muted"]}">+{len(items) - limit} more</li>' if len(items) > limit else ""
    return f'<ul style="padding-left:18px;margin:0;font-size:13px">{"".join(rows)}{more}</ul>'


def build(items: list[dict], ctx: dict) -> tuple[str, str, str]:
    """Returns (subject, html, text)."""
    tiers = {t: [i for i in items if i["tier"] == t] for t in TIER_TITLES}
    jobs = [i for i in items if i.get("job")]
    events = ctx.get("events", [])
    bills = [i for i in items if i.get("bill")]
    manual = ctx.get("manual_unsubs", [])
    cleanup = ctx.get("cleanup", {}) or {}
    new_review = ctx.get("new_review_senders", 0)
    sheet_url = ctx.get("sheet_url", "")

    bits = []
    if tiers["priority"]:
        bits.append(f"{len(tiers['priority'])} need you")
    if jobs:
        bits.append(f"{len(jobs)} job update{'s' if len(jobs) != 1 else ''}")
    if tiers["worth_a_look"]:
        bits.append(f"{len(tiers['worth_a_look'])} worth a look")
    subject = f"[Digest] {ctx.get('slot_label', '')}: " + (", ".join(bits) if bits else "nothing urgent")

    parts = []
    parts.append(f'<div style="font-size:13px;color:{C["muted"]}">{escape(ctx.get("date_label", ""))} · '
                 f'{len(items)} new email{"s" if len(items) != 1 else ""} sorted</div>')

    if tiers["priority"]:
        parts.append(_section(TIER_TITLES["priority"], "".join(_item(i) for i in tiers["priority"]), len(tiers["priority"])))
    else:
        parts.append(_section(TIER_TITLES["priority"], f'<div style="color:{C["muted"]};font-size:14px">Nothing needs you right now.</div>'))

    if jobs:
        rows = "".join(
            f'<tr><td style="padding:4px 10px 4px 0"><b>{escape(i["job"].get("company") or _sender(i))}</b></td>'
            f'<td style="padding:4px 10px 4px 0">{escape(i["job"].get("role") or "")}</td>'
            f'<td style="padding:4px 0"><a href="{thread_link(i["thread_id"])}" style="color:{C["blue"]}">'
            f'{escape(JOB_STATUS.get(i["job"].get("status"), "Update"))}</a></td></tr>' for i in jobs)
        tracker = (f'<div style="font-size:12px;margin-top:6px"><a href="{sheet_url}" style="color:{C["blue"]}">Open job tracker</a></div>'
                   if sheet_url else "")
        parts.append(_section("Job search", f'<table style="font-size:14px;border-collapse:collapse">{rows}</table>{tracker}', len(jobs)))

    if events:
        rows = "".join(
            f'<div style="margin:0 0 10px;font-size:14px"><b>{escape(ev["title"])}</b> · {escape(ev["when"])}'
            f'{(" · " + escape(ev["where"])) if ev.get("where") else ""}<br>'
            + (f'<a href="{ev["cal"]}" style="color:{C["blue"]}">Add to calendar</a> · ' if ev.get("cal") else "")
            + f'<a href="{thread_link(ev["thread_id"])}" style="color:{C["blue"]}">email</a></div>'
            for ev in events)
        parts.append(_section("Suggested for your calendar", rows, len(events)))

    if tiers["worth_a_look"]:
        parts.append(_section(TIER_TITLES["worth_a_look"], "".join(_item(i) for i in tiers["worth_a_look"]), len(tiers["worth_a_look"])))

    if bills:
        rows = "".join(
            f'<tr><td style="padding:4px 10px 4px 0"><b>{escape(i["bill"].get("vendor") or _sender(i))}</b></td>'
            f'<td style="padding:4px 10px 4px 0">{escape((i["bill"].get("kind") or "").title())}</td>'
            f'<td style="padding:4px 10px 4px 0">{escape(i["bill"].get("amount") or "")}</td>'
            f'<td style="padding:4px 0">{("due " + escape(i["bill"]["due"])) if i["bill"].get("due") else ""}</td></tr>'
            for i in bills)
        parts.append(_section("Bills & receipts", f'<table style="font-size:14px;border-collapse:collapse">{rows}</table>', len(bills)))

    if manual:
        rows = "".join(f'<li style="margin:0 0 4px"><a href="{escape(m["url"])}" style="color:{C["blue"]}">'
                       f'{escape(m["sender"])}</a></li>' for m in manual)
        parts.append(_section("Finish unsubscribing (one tap each)",
                              f'<div style="font-size:13px;color:{C["muted"]};margin-bottom:6px">These senders only offer a web page. '
                              f'Their mail is already deleted and filtered either way.</div>'
                              f'<ul style="padding-left:18px;margin:0;font-size:14px">{rows}</ul>', len(manual)))

    if cleanup.get("senders") or new_review:
        lines = []
        if cleanup.get("senders"):
            lines.append(f'Deleted {cleanup.get("trashed", 0):,} emails from {cleanup["senders"]} sender'
                         f'{"s" if cleanup["senders"] != 1 else ""} you approved.')
        if new_review:
            lines.append(f'{new_review} new sender{"s" if new_review != 1 else ""} added to your review list.')
        link = f' <a href="{sheet_url}" style="color:{C["blue"]}">Review senders</a>' if sheet_url else ""
        parts.append(_section("Cleanup", f'<div style="font-size:14px">{" ".join(lines)}{link}</div>'))

    auto = ctx.get("auto_cleaned") or {}
    recovered = ctx.get("recovered") or []
    if auto.get("count") or recovered:
        body = ""
        if recovered:
            body += (f'<div style="font-size:13px;margin-bottom:8px">You recovered mail from '
                     f'<b>{escape(", ".join(recovered[:8]))}</b>. Those senders won\'t be auto-deleted again.</div>')
        if auto.get("count"):
            preview = any(i.get("source") == "preview" for i in auto.get("items", []))
            body += (f'<div style="font-size:13px;color:{C["muted"]};margin-bottom:8px">'
                     + ("Preview: these would be moved to Trash if auto-clean were on." if preview else
                        "Moved to Trash, where they stay for 30 days. Tap <b>Recover</b>, then "
                        "<b>Move to Inbox</b> in Gmail. The scanner notices and never auto-deletes that sender again.")
                     + '</div>')
            rows = []
            for it in auto.get("items", [])[:30]:
                rows.append(f'<li style="margin:0 0 6px"><b>{escape(it["sender"])}</b> '
                            f'<span style="color:{C["muted"]}">· {escape(it["subject"])}</span><br>'
                            f'<span style="font-size:12px;color:{C["muted"]}">{escape(it.get("why", ""))}</span> '
                            f'<a href="{escape(it["link"])}" style="color:{C["blue"]};font-size:12px">Recover</a></li>')
            listed = min(len(auto.get("items", [])), 30)
            more = auto["count"] - listed
            all_link = ("https://mail.google.com/mail/u/0/#search/"
                        "label%3Aauto-cleaned+in%3Atrash")
            tail = ('<div style="font-size:13px;margin-top:4px">'
                    + (f'+{more:,} more · ' if more > 0 else "")
                    + f'<a href="{all_link}" style="color:{C["blue"]}">See everything auto-deleted</a></div>')
            body += f'<ul style="padding-left:18px;margin:0;font-size:13px">{"".join(rows)}</ul>{tail}'
        parts.append(_section("Auto-deleted", body, auto.get("count") or None))

    for t in ("low", "junk"):
        if tiers[t]:
            parts.append(_section(TIER_TITLES[t], _compact(tiers[t]), len(tiers[t])))

    html = (f'<div style="background:{C["bg"]};padding:18px 0">'
            f'<div style="max-width:620px;margin:0 auto;background:#fff;border:1px solid {C["line"]};'
            f'border-radius:8px;padding:20px 22px;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
            f'color:{C["text"]};line-height:1.4">'
            f'<div style="font-size:20px;font-weight:700">Email digest · {escape(ctx.get("slot_label", ""))}</div>'
            + "".join(parts)
            + f'<div style="margin-top:28px;font-size:11px;color:{C["muted"]}">Sorted by your email scanner. '
            + ("Auto-deleted mail stays in Trash for 30 days." if auto.get("count")
               else "Nothing here was deleted except senders you approved.")
            + '</div></div></div>')

    # plain text version
    t = [subject, ""]
    for tier in ("priority", "worth_a_look"):
        if tiers[tier]:
            t.append(TIER_TITLES[tier].upper())
            for i in tiers[tier]:
                t.append(f"- {_sender(i)}: {i['subject']}\n  {i.get('summary', '')}\n  {thread_link(i['thread_id'])}")
            t.append("")
    for ev in events:
        t.append(f"EVENT: {ev['title']} - {ev['when']} {ev.get('cal', '')}")
    for m in manual:
        t.append(f"UNSUBSCRIBE: {m['sender']} {m['url']}")
    if auto.get("count"):
        t.append(f"AUTO-DELETED: {auto['count']} (in Trash 30 days)")
        for it in auto.get("items", [])[:30]:
            t.append(f"- {it['sender']}: {it['subject']}  Recover: {it['link']}")
    for tier in ("low", "junk"):
        if tiers[tier]:
            t.append(f"{TIER_TITLES[tier]}: {len(tiers[tier])}")
    return subject, html, "\n".join(t)
