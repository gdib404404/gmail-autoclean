"""Thin wrapper around the Gmail API with batching and retry."""
from __future__ import annotations

import base64
import logging
import time
from email.message import EmailMessage
from typing import Callable, Iterable

import httplib2
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

META_HEADERS = [
    "From", "To", "Cc", "Subject", "Date", "Message-ID",
    "List-Unsubscribe", "List-Unsubscribe-Post", "Precedence", "Auto-Submitted",
]
RETRYABLE = {429, 500, 502, 503, 504}
# Dropped connections (e.g. "ssl.SSLEOFError: EOF occurred in violation of protocol")
NETWORK_ERRORS = (OSError, httplib2.HttpLib2Error)


def chunks(seq, n):
    seq = list(seq)
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def header(msg: dict, name: str) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _status(exc: Exception) -> int | None:
    if isinstance(exc, HttpError):
        return getattr(exc.resp, "status", None)
    return None


def _is_quota(exc: Exception) -> bool:
    """Per-minute quota (needs a longer wait) vs. too-many-at-once (short wait)."""
    text = str(exc).lower()
    return _status(exc) == 403 or "quota" in text


def _retryable(exc: Exception) -> bool:
    """429/5xx, plus Gmail's 403 'rateLimitExceeded' (per-minute quota)."""
    st = _status(exc)
    if st in RETRYABLE:
        return True
    if st == 403:
        text = str(exc).lower()
        return "ratelimitexceeded" in text or "quota exceeded" in text
    return False


class _Throttle:
    """Keeps us under Gmail's per-user quota (15,000 units/minute).

    Costs: messages.get / list = 5 units, batchModify = 50, send = 100.
    We budget ~150 units/sec (9,000/min) to leave plenty of headroom.
    """

    def __init__(self, units_per_sec: float = 150):
        self.rate = units_per_sec
        self.next_free = time.monotonic()

    def spend(self, units: float) -> None:
        now = time.monotonic()
        if self.next_free > now:
            time.sleep(self.next_free - now)
            now = self.next_free
        self.next_free = now + units / self.rate

    def cool_down(self, seconds: float) -> None:
        self.next_free = max(self.next_free, time.monotonic() + seconds)


THROTTLE = _Throttle()


def _call(req, units: float = 5, tries: int = 8):
    """Execute a single request, paced, with backoff on rate limits."""
    delay = 15.0
    for attempt in range(tries):
        THROTTLE.spend(units)
        try:
            return req.execute(num_retries=3)
        except HttpError as e:
            if _retryable(e) and attempt < tries - 1:
                log.info("Gmail rate limit hit; waiting %ds", delay)
                THROTTLE.cool_down(delay)
                delay = min(delay * 2, 65)
                continue
            raise
        except NETWORK_ERRORS as e:
            if attempt < tries - 1:
                log.info("Network hiccup (%s); retrying", type(e).__name__)
                THROTTLE.cool_down(5 * (attempt + 1))
                continue
            raise


class Gmail:
    def __init__(self, creds):
        self.svc = build("gmail", "v1", credentials=creds, cache_discovery=False)
        self.users = self.svc.users()
        self._labels: dict[str, str] | None = None
        self._me: str | None = None

    # ---------- basics ----------
    def me(self) -> str:
        if not self._me:
            self._me = _call(self.users.getProfile(userId="me"))["emailAddress"].lower()
        return self._me

    def list_ids(self, query: str, limit: int | None = None,
                 progress: Callable[[int], None] | None = None) -> list[str]:
        """All message IDs matching a Gmail search (newest first). Excludes spam/trash."""
        ids: list[str] = []
        token = None
        while True:
            resp = _call(self.users.messages().list(
                userId="me", q=query, maxResults=500, pageToken=token))
            ids.extend(m["id"] for m in resp.get("messages", []))
            if progress:
                progress(len(ids))
            token = resp.get("nextPageToken")
            if not token or (limit and len(ids) >= limit):
                break
        return ids[:limit] if limit else ids

    def get_batch(self, ids: Iterable[str], fmt: str = "metadata",
                  headers: list[str] | None = None,
                  progress: Callable[[int, int], None] | None = None,
                  batch_size: int = 20) -> dict[str, dict]:
        """Fetch many messages using small batched HTTP requests with retry.

        Batches of 20: Gmail runs a batch's requests in parallel and throttles
        users with too many in flight, so big batches get mostly rejected.

        Paced by THROTTLE (~30 messages/sec), which stays well under Gmail's
        per-user quota, and backs off if Google still says slow down.
        """
        headers = headers or META_HEADERS
        pending = list(dict.fromkeys(ids))
        total = len(pending)
        results: dict[str, dict] = {}
        rounds = 0
        while pending and rounds < 10:
            rounds += 1
            retry: list[str] = []
            for chunk in chunks(pending, batch_size):
                failed_here: list[str] = []
                quota_hit: list[bool] = []

                def cb(req_id, resp, exc, _failed=failed_here, _quota=quota_hit):
                    if exc is None:
                        results[req_id] = resp
                    elif _retryable(exc):
                        _failed.append(req_id)
                        if _is_quota(exc):
                            _quota.append(True)
                    else:
                        log.debug("skip %s: %s", req_id, exc)  # e.g. 404, message gone

                batch = self.svc.new_batch_http_request(callback=cb)
                for mid in chunk:
                    kwargs = {"userId": "me", "id": mid, "format": fmt}
                    if fmt == "metadata":
                        kwargs["metadataHeaders"] = headers
                    batch.add(self.users.messages().get(**kwargs), request_id=mid)
                THROTTLE.spend(5 * len(chunk))
                try:
                    batch.execute()
                except HttpError as e:
                    if _retryable(e):
                        failed_here.extend(m for m in chunk if m not in results)
                        if _is_quota(e):
                            quota_hit.append(True)
                    else:
                        raise
                except NETWORK_ERRORS as e:
                    log.info("Network hiccup (%s); will retry those", type(e).__name__)
                    failed_here.extend(m for m in chunk if m not in results)
                if failed_here:
                    retry.extend(failed_here)
                    # quota: let the minute window refill; concurrency: just a short breather
                    THROTTLE.cool_down(20 if quota_hit else 2)
                if progress:
                    progress(len(results), total)
            pending = retry
            if pending:
                log.info("Retrying %d rate-limited messages", len(pending))
                THROTTLE.cool_down(10)
        return results

    def get_full(self, mid: str) -> dict:
        return _call(self.users.messages().get(userId="me", id=mid, format="full"))

    # ---------- labels ----------
    def label_id(self, name: str) -> str:
        if self._labels is None:
            resp = _call(self.users.labels().list(userId="me"))
            self._labels = {l["name"]: l["id"] for l in resp.get("labels", [])}
        if name in self._labels:
            return self._labels[name]
        # create parents first so nested labels show properly
        if "/" in name:
            parent = name.rsplit("/", 1)[0]
            if parent not in self._labels:
                self.label_id(parent)
        created = _call(self.users.labels().create(userId="me", body={
            "name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"}))
        self._labels[name] = created["id"]
        return created["id"]

    def modify(self, ids: list[str], add: list[str] | None = None,
               remove: list[str] | None = None) -> None:
        for chunk in chunks(ids, 1000):
            _call(self.users.messages().batchModify(userId="me", body={
                "ids": chunk, "addLabelIds": add or [], "removeLabelIds": remove or []}), units=50)

    def trash(self, ids: list[str]) -> int:
        """Move messages to Trash (Gmail empties Trash after 30 days)."""
        if not ids:
            return 0
        try:
            self.modify(ids, add=["TRASH"], remove=["INBOX", "UNREAD"])
        except HttpError as e:
            if _status(e) != 400:
                raise
            log.info("batchModify TRASH refused, falling back to per-message trash")
            for chunk in chunks(ids, 50):
                batch = self.svc.new_batch_http_request()
                for mid in chunk:
                    batch.add(self.users.messages().trash(userId="me", id=mid))
                THROTTLE.spend(5 * len(chunk))
                for attempt in range(4):
                    try:
                        batch.execute()
                        break
                    except NETWORK_ERRORS:
                        if attempt == 3:
                            raise
                        THROTTLE.cool_down(5 * (attempt + 1))
        return len(ids)

    # ---------- filters ----------
    # Gmail caps accounts at ~1,000 filters, so blocked senders are packed into
    # shared filters ("a@x.com OR b@y.com OR ...") instead of one filter each.
    FILTER_MAX_CHARS = 900

    @staticmethod
    def _split_targets(from_value: str) -> set[str]:
        v = (from_value or "").strip().strip("(){}")
        return {t.strip().strip("(){}").lower() for t in v.split(" OR ") if t.strip()}

    def blocked_targets(self) -> set[str]:
        """Every sender/domain already covered by one of our delete filters."""
        resp = _call(self.users.settings().filters().list(userId="me"))
        out: set[str] = set()
        for f in resp.get("filter", []):
            if "TRASH" in (f.get("action", {}).get("addLabelIds") or []):
                out |= self._split_targets(f.get("criteria", {}).get("from", ""))
        return out

    def _create_filter(self, criteria: dict) -> str:
        body = {"criteria": criteria,
                "action": {"addLabelIds": ["TRASH"], "removeLabelIds": ["INBOX", "UNREAD"]}}
        try:
            _call(self.users.settings().filters().create(userId="me", body=body))
            return "delete filter added"
        except HttpError as e:
            if _status(e) != 400:
                raise
            # Fallback: skip inbox + mark read + label. Shouldn't normally happen.
            lid = self.label_id("Digest/Blocked")
            _call(self.users.settings().filters().create(userId="me", body={
                "criteria": criteria,
                "action": {"addLabelIds": [lid], "removeLabelIds": ["INBOX", "UNREAD"]}}))
            return "skip-inbox filter added (Gmail refused delete action)"

    def create_delete_filters(self, targets: list[str], domain_excludes: dict[str, list[str]] | None = None) -> dict[str, str]:
        """Send future mail from each target straight to Trash. Returns target -> note.

        Targets with exclusions (a whole domain minus your Always Important / Keep
        senders) get their own filter; everything else is packed together.
        """
        domain_excludes = domain_excludes or {}
        existing = self.blocked_targets()
        notes: dict[str, str] = {}
        plain: list[str] = []
        for t in dict.fromkeys(x.lower() for x in targets):
            if t in existing:
                notes[t] = "filter already existed"
            elif domain_excludes.get(t):
                crit = {"from": t, "negatedQuery": "from:(" + " OR ".join(domain_excludes[t]) + ")"}
                notes[t] = self._create_filter(crit)
            else:
                plain.append(t)
        group: list[str] = []

        def flush():
            if group:
                note = self._create_filter({"from": " OR ".join(group)})
                for g in group:
                    notes[g] = note
                group.clear()

        for t in plain:
            if group and len(" OR ".join(group + [t])) > self.FILTER_MAX_CHARS:
                flush()
            group.append(t)
        flush()
        return notes

    # ---------- sending ----------
    def send(self, to: str, subject: str, text: str, html: str | None = None) -> dict:
        msg = EmailMessage()
        msg["To"] = to
        msg["From"] = self.me()
        msg["Subject"] = subject
        msg.set_content(text)
        if html:
            msg.add_alternative(html, subtype="html")
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        return _call(self.users.messages().send(userId="me", body={"raw": raw}), units=100)


def extract_body(msg: dict, limit: int = 1500) -> str:
    """Best-effort plain text body from a format=full message."""
    import html as htmlmod
    import re

    plain, html_parts = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime in ("text/plain", "text/html"):
            try:
                text = base64.urlsafe_b64decode(data + "===").decode("utf-8", "replace")
            except Exception:
                text = ""
            (plain if mime == "text/plain" else html_parts).append(text)
        for p in part.get("parts", []) or []:
            walk(p)

    walk(msg.get("payload", {}))
    if plain:
        text = "\n".join(plain)
    elif html_parts:
        text = "\n".join(html_parts)
        text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
        text = re.sub(r"(?s)<br\s*/?>|</p>|</div>|</tr>", "\n", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = htmlmod.unescape(text)
    else:
        text = msg.get("snippet", "")
    text = re.sub(r"[ \t ‌​]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text).strip()
    return text[:limit]


def has_attachment(msg: dict) -> bool:
    """True if a format=full message has a real attached file."""
    def walk(part):
        if part.get("filename") and part.get("body", {}).get("attachmentId"):
            return True
        return any(walk(p) for p in part.get("parts", []) or [])
    return walk(msg.get("payload", {}))


def message_id(msg: dict) -> str:
    for h in msg.get("payload", {}).get("headers", []):
        if h.get("name", "").lower() == "message-id":
            return h.get("value", "").strip().strip("<>")
    return ""
