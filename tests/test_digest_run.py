"""End-to-end digest run against fake Gmail / Sheet / Claude."""
import sys
import time
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from emailscanner import classify as C
from emailscanner import digest
from emailscanner.config import Config
from fakes import FakeGmail, FakeSheet, make_msg


class Client:
    def __init__(self):
        self.messages = self

    def create(self, **kw):
        prompt = kw["messages"][0]["content"]
        ratings = []
        for i, block in enumerate(prompt.split("<email index=")[1:]):
            if "recruit" in block:
                ratings.append({"index": i, "tier": "priority", "reason": "Recruiter wants a call", "summary": "Acme wants to talk Thursday.",
                                "category": "job", "action_needed": True,
                                "job": {"company": "Acme", "role": "Engineer", "status": "interview"},
                                "event": {"title": "Call with Acme", "start": "2026-09-24T15:00:00", "all_day": False}})
            elif "bill" in block:
                ratings.append({"index": i, "tier": "worth_a_look", "reason": "Bill due", "summary": "Electric bill $84 due 10/1.",
                                "category": "bill", "bill": {"vendor": "City Power", "kind": "bill", "amount": "$84.00", "due": "2026-10-01"}})
            else:
                ratings.append({"index": i, "tier": "junk", "reason": "Promo", "summary": "Sale.", "category": "promo"})
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"ratings": ratings})])


def test_full_run(monkeypatch, tmp_path):
    now_ms = int(time.time() * 1000)
    g = FakeGmail([
        make_msg("1", "Jane <jane@acme.com>", "recruiting: engineer role", now_ms - 3_600_000),
        make_msg("2", "City Power <billing@citypower.com>", "Your bill is ready", now_ms - 7_200_000),
        make_msg("3", "Deals <promo@deals.com>", "50% off", now_ms - 100_000, unsub="<https://d/u>",
                 unsub_post="List-Unsubscribe=One-Click"),
    ])
    sh = FakeSheet()
    sh.append("Review Senders", [{"Sender": "old@spam.com", "Decision": "Delete"}])
    g.msgs["9"] = make_msg("9", "old@spam.com", "old", now_ms - 10 * 86_400_000, labels=("UNREAD",))

    monkeypatch.setattr(digest, "get_credentials", lambda: None)
    monkeypatch.setattr(digest, "Gmail", lambda creds: g)
    monkeypatch.setattr(digest.S, "Sheet", lambda creds, sid: sh)
    real = C.classify
    monkeypatch.setattr(digest, "classify", lambda *a, **k: real(*a, client=Client(), **k))
    monkeypatch.setattr(digest, "ROOT", tmp_path)
    # don't hit the network for the one-click unsubscribe
    monkeypatch.setattr("emailscanner.backlog.do_unsub", lambda p, gm: (True, "unsubscribed (one-click)"))

    cfg = Config(sheet_id="x", about_me="Test user")
    digest.run(cfg, force=True)

    # decisions applied first
    assert "TRASH" in g.msgs["9"]["labelIds"]
    # labels
    prio = g.labels["!Priority"]
    assert prio in g.msgs["1"]["labelIds"] and "STARRED" in g.msgs["1"]["labelIds"]
    assert g.labels["Digest/Probably junk"] in g.msgs["3"]["labelIds"]
    assert "INBOX" in g.msgs["3"]["labelIds"]  # nothing archived/deleted by default
    # digest sent
    assert len(g.sent) == 1
    d = g.sent[0]
    assert d["subject"].startswith("[Digest]") and "1 need you" in d["subject"] and "1 job update" in d["subject"]
    assert "Add to calendar" in d["html"] and "City Power" in d["html"]
    # trackers
    jobs = sh.read("Job Tracker")
    assert jobs[0]["Company"] == "Acme" and jobs[0]["Status"] == "Interview"
    assert sh.read("Events")[0]["Title"] == "Call with Acme"
    assert sh.read("Bills & Receipts")[0]["Amount"] == "$84.00"
    assert any(r["Sender"] == "promo@deals.com" and r["Source"] == "digest" for r in sh.read("Review Senders"))
    assert len(sh.read("Log")) == 3
    assert sh.get_state("last_slot")

    # second run: nothing new (digest itself and rated mail are skipped)
    digest.run(cfg, force=True)
    assert len(g.sent) == 1

    # user adds !Priority to the junk email -> learned "up"
    g.msgs["3"]["labelIds"].append(prio)
    digest.run(cfg, force=True)
    learned = sh.read("Learned")
    assert learned and learned[0]["Sender"] == "promo@deals.com" and learned[0]["Direction"] == "up"


def test_preview_writes_nothing(monkeypatch, tmp_path):
    now_ms = int(time.time() * 1000)
    g = FakeGmail([make_msg("1", "jane@acme.com", "recruiting call", now_ms - 1000)])
    sh = FakeSheet()
    monkeypatch.setattr(digest, "get_credentials", lambda: None)
    monkeypatch.setattr(digest, "Gmail", lambda creds: g)
    monkeypatch.setattr(digest.S, "Sheet", lambda creds, sid: sh)
    real = C.classify
    monkeypatch.setattr(digest, "classify", lambda *a, **k: real(*a, client=Client(), **k))
    monkeypatch.setattr(digest, "ROOT", tmp_path)
    digest.run(Config(sheet_id="x"), preview=True)
    assert not g.sent and not sh.read("Log") and (tmp_path / "digest_preview.html").exists()
    assert "!Priority" not in [l for m in g.msgs.values() for l in m["labelIds"]]


def test_digest_auto_clean_and_recover_link(monkeypatch, tmp_path):
    from test_sweep import _training_sheet

    class JunkClient(Client):
        def create(self, **kw):
            r = super().create(**kw)
            for x in r.content[0].input["ratings"]:
                if x["tier"] == "junk":
                    x["junk_certainty"] = 97
            return r

    now_ms = int(time.time() * 1000)
    g = FakeGmail([
        make_msg("1", "Deals <promo@deals.com>", "50% off everything", now_ms - 100_000, unsub="<https://d/u>",
                 unsub_post="List-Unsubscribe=One-Click", labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS")),
        make_msg("2", "Jane <jane@acme.com>", "recruiting: engineer role", now_ms - 50_000),
    ])
    sh = _training_sheet()
    monkeypatch.setattr(digest, "get_credentials", lambda: None)
    monkeypatch.setattr(digest, "Gmail", lambda creds: g)
    monkeypatch.setattr(digest.S, "Sheet", lambda creds, sid: sh)
    real = C.classify
    monkeypatch.setattr(digest, "classify", lambda *a, **k: real(*a, client=JunkClient(), **k))
    monkeypatch.setattr(digest, "ROOT", tmp_path)
    digest.run(Config(sheet_id="x", about_me="Test user", auto_clean=True), force=True)
    assert "TRASH" in g.msgs["1"]["labelIds"] and "TRASH" not in g.msgs["2"]["labelIds"]
    html = g.sent[0]["html"]
    assert "Auto-deleted" in html and "Recover" in html and "50% off everything" in html
    assert sh.get_state("auto_cleaned_since_digest")["count"] == 0   # reset after sending

    # off by default: nothing trashed
    g2 = FakeGmail([make_msg("1", "Deals <promo@deals.com>", "50% off everything", now_ms - 100_000,
                             unsub="<https://d/u>", unsub_post="List-Unsubscribe=One-Click",
                             labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS"))])
    monkeypatch.setattr(digest, "Gmail", lambda creds: g2)
    digest.run(Config(sheet_id="x", about_me="Test user"), force=True)
    assert "TRASH" not in g2.msgs["1"]["labelIds"]
