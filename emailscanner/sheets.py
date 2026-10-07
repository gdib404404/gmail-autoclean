"""Google Sheet used as the review screen, trackers, and small state store."""
from __future__ import annotations

import json
import logging
import time

from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger(__name__)

REVIEW = "Review Senders"
ARCHIVE = "Deleted Senders"
SWEEP = "Sweep Preview"
SWEEP_SUMMARY = "Sweep Summary"
IMPORTANT = "Always Important"
JOBS = "Job Tracker"
EVENTS = "Events"
BILLS = "Bills & Receipts"
LOG = "Log"
LEARNED = "Learned"
STATE = "State"

DECISIONS = ["Delete", "Delete (no unsubscribe)", "Delete whole domain", "Keep", "Later"]

TABS: dict[str, list[str]] = {
    REVIEW: ["Decision", "Sender", "Name", "Domain", "Emails", "Unread", "Domain total",
             "Last email", "First email", "Sample subjects", "You replied?",
             "Unsubscribe", "Looks spammy?", "Source", "Status", "Processed at"],
    ARCHIVE: ["Decision", "Sender", "Name", "Domain", "Emails", "Unread", "Domain total",
              "Last email", "First email", "Sample subjects", "You replied?",
              "Unsubscribe", "Looks spammy?", "Source", "Status", "Processed at"],
    IMPORTANT: ["Sender or domain", "Note"],
    SWEEP: ["Action", "Sender", "Name", "Emails", "Unread", "Confidence", "Why", "Model says delete",
            "Mixed sender?", "Sample subjects"],
    SWEEP_SUMMARY: ["Item", "Value"],
    JOBS: ["Company", "Role", "Status", "Last update", "First seen", "Latest note", "Email link"],
    EVENTS: ["Found", "Title", "When", "Where", "Add to calendar", "Email link"],
    BILLS: ["Date", "Vendor", "Type", "Amount", "Due", "Email link"],
    LOG: ["Message ID", "Thread ID", "Sender", "Subject", "Tier", "Rated at", "Correction"],
    LEARNED: ["Sender", "Direction", "Example subject", "Date"],
    STATE: ["Key", "Value"],
}


def _col(n: int) -> str:
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    return s


def _call(req, tries: int = 6):
    import httplib2
    delay = 2.0
    for attempt in range(tries):
        try:
            return req.execute(num_retries=3)
        except HttpError as e:
            if getattr(e.resp, "status", None) in (429, 500, 502, 503, 504) and attempt < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise
        except (OSError, httplib2.HttpLib2Error):
            if attempt < tries - 1:
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise


class Sheet:
    def __init__(self, creds, sheet_id: str):
        if not sheet_id:
            raise SystemExit("SHEET_ID is not set. Run `python -m emailscanner init-sheet` first.")
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        self.id = sheet_id
        self._state: dict[str, str] | None = None

    @property
    def url(self) -> str:
        return f"https://docs.google.com/spreadsheets/d/{self.id}/edit"

    # ---------- creation ----------
    @staticmethod
    def create(creds, title: str = "Email Scanner") -> str:
        svc = build("sheets", "v4", credentials=creds, cache_discovery=False)
        body = {"properties": {"title": title},
                "sheets": [{"properties": {"title": t, "gridProperties": {"frozenRowCount": 1}}}
                           for t in TABS]}
        resp = _call(svc.spreadsheets().create(body=body))
        sid = resp["spreadsheetId"]
        sheet = Sheet(creds, sid)
        for tab, cols in TABS.items():
            sheet._write_range(f"'{tab}'!A1", [cols])
        sheet.format_tabs()
        return sid

    def _tab_ids(self) -> dict[str, int]:
        meta = _call(self.svc.spreadsheets().get(spreadsheetId=self.id, fields="sheets.properties"))
        return {s["properties"]["title"]: s["properties"]["sheetId"] for s in meta["sheets"]}

    def ensure_tabs(self) -> None:
        ids = self._tab_ids()
        missing = [t for t in TABS if t not in ids]
        if missing:
            _call(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.id, body={"requests": [
                {"addSheet": {"properties": {"title": t, "gridProperties": {"frozenRowCount": 1}}}}
                for t in missing]}))
            for t in missing:
                self._write_range(f"'{t}'!A1", [TABS[t]])
            self.format_tabs()

    def format_tabs(self) -> None:
        ids = self._tab_ids()
        reqs = []
        for tab, sid in ids.items():
            if tab not in TABS:
                continue
            reqs.append({"repeatCell": {
                "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
                "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                               "backgroundColor": {"red": .93, "green": .93, "blue": .93}}},
                "fields": "userEnteredFormat(textFormat,backgroundColor)"}})
        rid = ids.get(REVIEW)
        if rid is not None:
            reqs.append({"setDataValidation": {
                "range": {"sheetId": rid, "startRowIndex": 1, "startColumnIndex": 0, "endColumnIndex": 1},
                "rule": {"condition": {"type": "ONE_OF_LIST",
                                       "values": [{"userEnteredValue": d} for d in DECISIONS]},
                         "showCustomUi": True, "strict": True}}})
            widths = [170, 260, 160, 150, 70, 70, 90, 100, 100, 420, 90, 110, 100, 80, 320, 140]
            for i, w in enumerate(widths):
                reqs.append({"updateDimensionProperties": {
                    "range": {"sheetId": rid, "dimension": "COLUMNS", "startIndex": i, "endIndex": i + 1},
                    "properties": {"pixelSize": w}, "fields": "pixelSize"}})
        _call(self.svc.spreadsheets().batchUpdate(spreadsheetId=self.id, body={"requests": reqs}))

    # ---------- generic IO ----------
    def _write_range(self, rng: str, values: list[list], raw: bool = False) -> None:
        _call(self.svc.spreadsheets().values().update(
            spreadsheetId=self.id, range=rng, valueInputOption="RAW" if raw else "USER_ENTERED",
            body={"values": values}))

    def read(self, tab: str) -> list[dict]:
        """Rows as dicts (header -> value), each with a '_row' (1-based sheet row)."""
        resp = _call(self.svc.spreadsheets().values().get(
            spreadsheetId=self.id, range=f"'{tab}'", valueRenderOption="FORMATTED_VALUE"))
        values = resp.get("values", [])
        if not values:
            return []
        head = values[0]
        rows = []
        for i, v in enumerate(values[1:], start=2):
            row = {h: (v[j] if j < len(v) else "") for j, h in enumerate(head)}
            row["_row"] = i
            rows.append(row)
        return rows

    def replace_all(self, tab: str, rows: list[dict], raw: bool = False) -> None:
        cols = TABS[tab]
        _call(self.svc.spreadsheets().values().clear(spreadsheetId=self.id, range=f"'{tab}'"))
        values = [cols] + [[_cell(r.get(c, ""), raw) for c in cols] for r in rows]
        for start in range(0, len(values), 5000):
            self._write_range(f"'{tab}'!A{start + 1}", values[start:start + 5000], raw=raw)

    def append(self, tab: str, rows: list[dict], raw: bool = False) -> None:
        """raw=True stores text exactly (no date/number/formula interpretation)."""
        if not rows:
            return
        cols = TABS[tab]
        _call(self.svc.spreadsheets().values().append(
            spreadsheetId=self.id, range=f"'{tab}'!A1", valueInputOption="RAW" if raw else "USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [[_cell(r.get(c, ""), raw) for c in cols] for r in rows]}))

    def update_cells(self, tab: str, row: int, values: dict) -> None:
        cols = TABS[tab]
        data = [{"range": f"'{tab}'!{_col(cols.index(k) + 1)}{row}", "values": [[_cell(v)]]}
                for k, v in values.items()]
        _call(self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.id, body={"valueInputOption": "USER_ENTERED", "data": data}))

    def update_many(self, tab: str, updates: list[tuple[int, dict]]) -> None:
        """Several row updates in a single API call (Sheets allows ~60 writes/min)."""
        if not updates:
            return
        cols = TABS[tab]
        data = [{"range": f"'{tab}'!{_col(cols.index(k) + 1)}{row}", "values": [[_cell(v)]]}
                for row, values in updates for k, v in values.items()]
        _call(self.svc.spreadsheets().values().batchUpdate(
            spreadsheetId=self.id, body={"valueInputOption": "USER_ENTERED", "data": data}))

    # ---------- state ----------
    def state(self) -> dict[str, str]:
        if self._state is None:
            self._state = {r["Key"]: r["Value"] for r in self.read(STATE) if r.get("Key")}
        return self._state

    def get_state(self, key: str, default=None):
        raw = self.state().get(key)
        if raw in (None, ""):
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return raw

    def set_state(self, **kv) -> None:
        st = self.state()
        for k, v in kv.items():
            st[k] = json.dumps(v)
        _call(self.svc.spreadsheets().values().clear(spreadsheetId=self.id, range=f"'{STATE}'"))
        # RAW so JSON isn't reinterpreted as numbers/dates
        _call(self.svc.spreadsheets().values().update(
            spreadsheetId=self.id, range=f"'{STATE}'!A1", valueInputOption="RAW",
            body={"values": [TABS[STATE]] + [[k, v] for k, v in st.items()]}))


def _cell(v, raw: bool = False):
    if v is None:
        return ""
    if isinstance(v, bool):
        return "Yes" if v else ""
    if isinstance(v, (int, float)):
        return v
    s = str(v)
    # stop Sheets from treating user text as a formula (links are built on purpose below)
    if not raw and s[:1] in ("=", "+", "-", "@") and not s.startswith("=HYPERLINK("):
        s = "'" + s
    return s[:49000]


def hyperlink(url: str, text: str) -> str:
    if not url:
        return ""
    u = url.replace('"', "%22")
    t = (text or "open").replace('"', "'")[:200]
    return f'=HYPERLINK("{u}","{t}")'
