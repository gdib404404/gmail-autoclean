"""In-memory stand-ins for Gmail and the Google Sheet, for tests."""
from __future__ import annotations

import json

from emailscanner import sheets as S


def make_msg(mid, frm, subject, date_ms, labels=("INBOX", "UNREAD"), unsub="", unsub_post="",
             thread=None, body="hello"):
    hdrs = [{"name": "From", "value": frm}, {"name": "Subject", "value": subject}]
    if unsub:
        hdrs.append({"name": "List-Unsubscribe", "value": unsub})
    if unsub_post:
        hdrs.append({"name": "List-Unsubscribe-Post", "value": unsub_post})
    import base64
    data = base64.urlsafe_b64encode(body.encode()).decode()
    return {"id": mid, "threadId": thread or f"t{mid}", "labelIds": list(labels), "internalDate": str(date_ms),
            "snippet": body[:50],
            "payload": {"headers": hdrs, "mimeType": "text/plain", "body": {"data": data}}}


class FakeGmail:
    def __init__(self, messages, me="me@gmail.com", sent_to=()):
        self.msgs = {m["id"]: m for m in messages}
        self._me = me
        self.sent = []
        self.filters = []
        self.labels = {}
        self.sent_to = set(sent_to)

    def me(self):
        return self._me

    def _from(self, m):
        from emailscanner.senders import parse_message
        return parse_message(m)["from_email"]

    def list_ids(self, query, limit=None, progress=None):
        live = [m for m in self.msgs.values() if "TRASH" not in m["labelIds"]]
        live.sort(key=lambda m: -int(m["internalDate"]))
        if query.startswith("label:"):
            lid = self.labels.get(query[6:])
            return [m["id"] for m in live if lid and lid in m["labelIds"]]
        if query.startswith("in:sent"):
            return [m["id"] for m in live if "SENT" in m["labelIds"]]
        if "has:attachment" in query:
            return [m["id"] for m in live if m.get("_attach")]
        if query.startswith("from:"):
            target = query[5:]
            out = [m["id"] for m in live if self._from(m) == target or self._from(m).endswith("." + target)
                   or self._from(m).endswith("@" + target) or target in self._from(m)]
        elif query.startswith("in:sent to:"):
            out = ["s1"] if query.split("to:")[1] in self.sent_to else []
        elif query.startswith("in:inbox"):
            out = [m["id"] for m in live if "INBOX" in m["labelIds"]]
        else:
            out = [m["id"] for m in live]
        return out[:limit] if limit else out

    def get_batch(self, ids, fmt="metadata", headers=None, progress=None):
        return {i: json.loads(json.dumps(self.msgs[i])) for i in ids if i in self.msgs}

    def label_id(self, name):
        return self.labels.setdefault(name, f"Label_{len(self.labels) + 1}")

    def modify(self, ids, add=None, remove=None):
        for i in ids:
            if i not in self.msgs:
                continue
            labs = self.msgs[i]["labelIds"]
            for a in add or []:
                if a not in labs:
                    labs.append(a)
            for r in remove or []:
                if r in labs:
                    labs.remove(r)

    def trash(self, ids):
        self.modify(ids, add=["TRASH"], remove=["INBOX", "UNREAD"])
        return len(ids)

    def create_delete_filters(self, targets, domain_excludes=None):
        domain_excludes = domain_excludes or {}
        for t in targets:
            self.filters.append((t, tuple(domain_excludes.get(t, ()))))
        return {t: "delete filter added" for t in targets}

    def send(self, to, subject, text, html=None):
        mid = f"sent{len(self.sent)}"
        self.sent.append({"to": to, "subject": subject, "text": text, "html": html})
        self.msgs[mid] = make_msg(mid, self._me, subject, 9_999_999_999_999, labels=("SENT", "INBOX"))
        return {"id": mid}


class FakeSheet:
    url = "https://docs.google.com/spreadsheets/d/FAKE/edit"

    def __init__(self):
        self.tabs = {t: [] for t in S.TABS}
        self._st = {}

    def ensure_tabs(self):
        pass

    def format_tabs(self):
        pass

    def read(self, tab):
        return [{**{c: r.get(c, "") for c in S.TABS[tab]}, "_row": i + 2} for i, r in enumerate(self.tabs[tab])]

    def append(self, tab, rows, raw=False):
        for r in rows:
            self.tabs[tab].append({c: r.get(c, "") for c in S.TABS[tab]})

    def replace_all(self, tab, rows, raw=False):
        self.tabs[tab] = [{c: r.get(c, "") for c in S.TABS[tab]} for r in rows]

    def update_cells(self, tab, row, values):
        self.tabs[tab][row - 2].update(values)

    def update_many(self, tab, updates):
        for row, values in updates:
            self.update_cells(tab, row, values)

    def get_state(self, key, default=None):
        return self._st.get(key, default)

    def set_state(self, **kv):
        self._st.update(json.loads(json.dumps(kv)))
