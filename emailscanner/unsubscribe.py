"""Unsubscribe using the List-Unsubscribe header (RFC 2369 / RFC 8058)."""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, unquote, urlparse

import requests

log = logging.getLogger(__name__)


def parse_uris(list_unsub: str) -> list[str]:
    uris = re.findall(r"<\s*([^>]+?)\s*>", list_unsub or "")
    if not uris and list_unsub:
        uris = [u.strip() for u in list_unsub.split(",") if u.strip()]
    return uris


def plan(list_unsub: str, list_unsub_post: str = "") -> dict:
    """Decide how to unsubscribe.

    one-click: HTTPS URL + List-Unsubscribe-Post: List-Unsubscribe=One-Click (automatic)
    mailto:    send an email to the address (automatic)
    link:      only a web page, needs a human click (shown in the digest)
    none:      no unsubscribe offered
    """
    uris = parse_uris(list_unsub)
    https = [u for u in uris if u.lower().startswith("https://")]
    http = [u for u in uris if u.lower().startswith("http://")]
    mailto = [u for u in uris if u.lower().startswith("mailto:")]
    one_click = "one-click" in (list_unsub_post or "").lower().replace(" ", "")
    if https and one_click:
        return {"method": "one-click", "url": https[0], "mailto": mailto[0] if mailto else ""}
    if mailto:
        return {"method": "mailto", "url": (https or http or [""])[0], "mailto": mailto[0]}
    if https or http:
        return {"method": "link", "url": (https or http)[0], "mailto": ""}
    return {"method": "none", "url": "", "mailto": ""}


def parse_mailto(uri: str) -> tuple[str, str, str]:
    p = urlparse(uri)
    to = unquote(p.path)
    q = parse_qs(p.query)
    subject = q.get("subject", ["unsubscribe"])[0]
    body = q.get("body", ["unsubscribe"])[0]
    return to, subject, body


def execute(p: dict, gmail=None, timeout: int = 20) -> tuple[bool, str]:
    """Returns (done_automatically, human readable note)."""
    method = p["method"]
    if method == "one-click":
        try:
            r = requests.post(p["url"], data={"List-Unsubscribe": "One-Click"}, timeout=timeout,
                              headers={"User-Agent": "email-scanner/1.0"}, allow_redirects=True)
            if r.status_code < 400:
                return True, "unsubscribed (one-click)"
            log.info("one-click unsubscribe returned %s", r.status_code)
        except requests.RequestException as e:
            log.info("one-click unsubscribe failed: %s", e)
        if p.get("mailto") and gmail:
            return execute({"method": "mailto", "mailto": p["mailto"], "url": p["url"]}, gmail)
        return False, "unsubscribe needs a click"
    if method == "mailto" and gmail:
        to, subject, body = parse_mailto(p["mailto"])
        if to:
            try:
                gmail.send(to, subject, body)
                return True, "unsubscribed (sent unsubscribe email)"
            except Exception as e:  # noqa: BLE001
                log.info("mailto unsubscribe failed: %s", e)
        return (False, "unsubscribe needs a click") if p.get("url") else (False, "unsubscribe failed")
    if method == "link":
        return False, "unsubscribe needs a click"
    return False, "no unsubscribe offered"
