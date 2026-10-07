import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import pytest

from emailscanner import backlog, classify, render, schedule, senders, unsubscribe
from emailscanner.config import Config
from fakes import FakeGmail, FakeSheet, make_msg

CT = ZoneInfo("America/Chicago")
DAY = 86_400_000


# ------------------------------------------------------------ senders
def test_parse_and_domains():
    assert senders.parse_from('"Quora Digest" <Digest-NoReply@Quora.com>') == ("Quora Digest", "digest-noreply@quora.com")
    assert senders.base_domain("x@mail.quora.com") == "quora.com"
    assert senders.base_domain("x@news.bbc.co.uk") == "bbc.co.uk"
    assert senders.domain_of("a@b.c.com") == "b.c.com"


def test_aggregate_and_merge():
    t = 1_750_000_000_000
    msgs = [senders.parse_message(m) for m in [
        make_msg("1", "Quora <digest@quora.com>", "Top stories", t, unsub="<https://q/u>", unsub_post="List-Unsubscribe=One-Click"),
        make_msg("2", "Quora <digest@quora.com>", "More stories", t + DAY, labels=("INBOX",)),
        make_msg("3", "Mom <mom@gmail.com>", "dinner?", t),
        make_msg("4", "Me <me@gmail.com>", "note to self", t),
    ]]
    rows = senders.aggregate(msgs, replied={"mom@gmail.com"}, me="me@gmail.com")
    assert [r["Sender"] for r in rows] == ["digest@quora.com", "mom@gmail.com"]
    q = rows[0]
    assert q["Emails"] == 2 and q["Unread"] == 1 and q["Unsubscribe"] == "one-click"
    assert q["Sample subjects"].startswith("More stories")
    assert rows[1]["You replied?"] == "Yes"

    existing = [{"Sender": "digest@quora.com", "Decision": "Delete", "Status": "", "_row": 2},
                {"Sender": "gone@old.com", "Decision": "Delete", "Status": "Done: trashed 5", "Emails": "5", "_row": 3}]
    merged = senders.merge_review(existing, rows)
    by = {r["Sender"]: r for r in merged}
    assert by["digest@quora.com"]["Decision"] == "Delete"
    assert "gone@old.com" in by                     # history kept
    assert merged[-1]["Sender"] == "gone@old.com"   # processed rows sink to the bottom


def test_spammy_hint():
    assert senders.looks_spammy("promo8821@deals-4u.xyz", "", False, False)
    assert not senders.looks_spammy("digest@quora.com", "Quora", True, False)
    assert not senders.looks_spammy("weird123456@x.xyz", "", False, True)  # you replied -> never


# ------------------------------------------------------------ unsubscribe
def test_unsub_plans():
    assert unsubscribe.plan("<https://a/u>, <mailto:u@a.com>", "List-Unsubscribe=One-Click")["method"] == "one-click"
    assert unsubscribe.plan("<https://a/u>, <mailto:u@a.com?subject=bye>", "")["method"] == "mailto"
    assert unsubscribe.plan("<https://a/u>", "")["method"] == "link"
    assert unsubscribe.plan("", "")["method"] == "none"
    assert unsubscribe.parse_mailto("mailto:u@a.com?subject=Remove%20me") == ("u@a.com", "Remove me", "unsubscribe")


def test_unsub_link_needs_click():
    ok, note = unsubscribe.execute({"method": "link", "url": "https://x", "mailto": ""})
    assert not ok and "click" in note


def test_unsub_mailto_sends():
    g = FakeGmail([])
    ok, _ = unsubscribe.execute(unsubscribe.plan("<mailto:leave@list.com?subject=unsub>"), g)
    assert ok and g.sent[0]["to"] == "leave@list.com"


# ------------------------------------------------------------ schedule / DST
@pytest.mark.parametrize("utc,expected", [
    ("2026-09-23T14:07", "2026-09-23@09"),   # CDT 9:07
    ("2026-09-23T15:07", "2026-09-23@09"),   # CDT 10:07 -> still slot 9 (deduped by state)
    ("2026-12-02T14:07", None),              # CST 8:07  -> too early
    ("2026-12-02T15:07", "2026-12-02@09"),   # CST 9:07
    ("2026-09-24T01:07", "2026-09-23@20"),   # CDT 8:07 PM
    ("2026-12-03T02:07", "2026-12-02@20"),   # CST 8:07 PM
    ("2026-09-23T19:50", "2026-09-23@14"),   # late GitHub start
    ("2026-09-23T17:00", None),
])
def test_slots(utc, expected):
    now = datetime.fromisoformat(utc + "+00:00").astimezone(CT)
    assert schedule.current_slot(now, [9, 14, 20]) == expected


def test_slot_label():
    assert schedule.slot_label("2026-09-23@09") == "9 AM"
    assert schedule.slot_label("2026-09-23@20") == "8 PM"


# ------------------------------------------------------------ render
def test_calendar_link():
    link = render.calendar_link({"title": "Interview", "start": "2026-09-25T14:00:00", "location": "Zoom"}, "America/Chicago")
    assert "dates=20260925T140000%2F20260925T150000" in link and "ctz=America%2FChicago" in link
    allday = render.calendar_link({"title": "Deadline", "start": "2026-09-30", "all_day": True}, "America/Chicago")
    assert "dates=20260930%2F20261001" in allday
    assert render.calendar_link({"title": "x", "start": "next week"}, "UTC") == ""
    assert render.human_when({"start": "2026-09-25T14:00:00"}) == "Fri Sep 25, 2:00 PM"


# ------------------------------------------------------------ classify
class FakeClient:
    def __init__(self, ratings=None, fail=False):
        self.ratings, self.fail, self.calls = ratings or [], fail, 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", input={"ratings": self.ratings})])


def _email(addr, **kw):
    return {"from_email": addr, "from_name": "", "subject": "s", "date_ms": 1_750_000_000_000,
            "snippet": "snip", "body": "b", **kw}


def test_classify_overrides_and_fallback():
    cfg = Config(about_me="me")
    emails = [_email("boss@work.com"), _email("friend@gmail.com", replied_before=True), _email("jobs@acme.com")]
    client = FakeClient([
        {"index": 0, "tier": "low", "reason": "r", "summary": "s", "category": "other"},
        {"index": 1, "tier": "junk", "reason": "r", "summary": "s", "category": "other"},
        {"index": 2, "tier": "junk", "reason": "r", "summary": "s", "category": "job",
         "job": {"company": "Acme", "role": "Engineer", "status": "interview"}},
    ])
    out = classify.classify(emails, cfg, CT, learned=[], important={"work.com"}, client=client)
    assert [o["tier"] for o in out] == ["priority", "low", "worth_a_look"]

    out = classify.classify(emails[:1], cfg, CT, [], set(), client=FakeClient(fail=True))
    assert out[0]["tier"] == "worth_a_look"   # never silently junked


# ------------------------------------------------------------ apply decisions
def test_apply_decisions():
    t = 1_750_000_000_000
    g = FakeGmail([
        make_msg("1", "digest@quora.com", "a", t, unsub="<https://q/u>"),
        make_msg("2", "digest@quora.com", "b", t + 1),
        make_msg("3", "promo@shop.com", "c", t),
        make_msg("4", "orders@shop.com", "d", t),
        make_msg("5", "news@shop.com", "e", t),
        make_msg("6", "rando@gmail.com", "f", t),
        make_msg("7", "keepme@quora.com", "g", t),
    ])
    sh = FakeSheet()
    sh.append("Review Senders", [
        {"Sender": "digest@quora.com", "Decision": "Delete"},
        {"Sender": "promo@shop.com", "Decision": "Delete whole domain"},
        {"Sender": "orders@shop.com", "Decision": "Keep"},
        {"Sender": "news@shop.com", "Decision": ""},
        {"Sender": "rando@gmail.com", "Decision": "Delete whole domain"},
        {"Sender": "keepme@quora.com", "Decision": "Later"},
    ])
    s = backlog.apply_decisions(g, sh)
    trashed = {i for i, m in g.msgs.items() if "TRASH" in m["labelIds"]}
    assert trashed == {"1", "2", "3", "5"}          # orders@shop.com kept, gmail.com refused, keepme untouched
    assert ("digest@quora.com", ()) in g.filters
    assert ("shop.com", ("orders@shop.com",)) in g.filters
    review = [r["Sender"] for r in sh.read("Review Senders")]
    rows = {r["Sender"]: r for r in sh.read("Review Senders") + sh.read("Deleted Senders")}
    # finished deletes moved out of the review tab; Keep sinks to the bottom
    assert set(r["Sender"] for r in sh.read("Deleted Senders")) == {"digest@quora.com", "promo@shop.com", "news@shop.com"}
    assert review == ["keepme@quora.com", "rando@gmail.com", "orders@shop.com"]
    assert rows["rando@gmail.com"]["Decision"] == ""     # skipped -> asks for a new choice
    assert rows["orders@shop.com"]["Status"] == "Kept"
    assert rows["news@shop.com"]["Status"].startswith("Covered by shop.com")
    assert rows["rando@gmail.com"]["Status"].startswith("Skipped")
    assert rows["keepme@quora.com"]["Status"] == ""
    assert s["manual_unsubs"] == [{"sender": "digest@quora.com", "url": "https://q/u"}]
    assert sh.get_state("cleanup_since_digest")["trashed"] == 4
    # running again does nothing new
    assert backlog.apply_decisions(g, sh)["senders"] == 0


def test_apply_respects_always_important():
    g = FakeGmail([make_msg("1", "hr@acme.com", "x", 1)])
    sh = FakeSheet()
    sh.append("Always Important", [{"Sender or domain": "acme.com"}])
    sh.append("Review Senders", [{"Sender": "hr@acme.com", "Decision": "Delete"}])
    backlog.apply_decisions(g, sh)
    assert "TRASH" not in g.msgs["1"]["labelIds"]
    assert sh.read("Review Senders")[0]["Status"].startswith("Skipped")


def test_filter_packing():
    from emailscanner.gmail import Gmail
    g = Gmail.__new__(Gmail)
    created = []
    g.blocked_targets = lambda: {"old@x.com"}
    g._create_filter = lambda crit: created.append(crit) or "delete filter added"
    targets = ["old@x.com"] + [f"sender{i:03d}@newsletter-domain-{i}.com" for i in range(60)] + ["shop.com"]
    notes = g.create_delete_filters(targets, domain_excludes={"shop.com": ["orders@shop.com"]})
    assert notes["old@x.com"] == "filter already existed"
    assert all(len(c["from"]) <= 900 for c in created)
    assert {"from": "shop.com", "negatedQuery": "from:(orders@shop.com)"} in created
    packed = [c for c in created if " OR " in c["from"]]
    assert 2 <= len(packed) <= 4                     # 60 senders -> a few filters, not 60
    assert sum(len(c["from"].split(" OR ")) for c in packed) == 60
    assert Gmail._split_targets("(a@b.com OR c.com)") == {"a@b.com", "c.com"}


def test_gmail_403_rate_limit_is_retried(monkeypatch):
    from googleapiclient.errors import HttpError
    from emailscanner import gmail as G
    monkeypatch.setattr(G.time, "sleep", lambda s: None)
    body = b'{"error":{"message":"Quota exceeded for quota metric","errors":[{"reason":"rateLimitExceeded"}]}}'
    err = HttpError(SimpleNamespace(status=403, reason="Forbidden"), body)
    calls = {"n": 0}

    class Req:
        def execute(self, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                raise err
            return {"ok": True}
    assert G._call(Req()) == {"ok": True} and calls["n"] == 3
    # a real permission error is not retried
    denied = HttpError(SimpleNamespace(status=403, reason="Forbidden"), b'{"error":{"message":"Insufficient Permission"}}')
    assert not G._retryable(denied)


def test_network_drop_is_retried(monkeypatch):
    import ssl
    from emailscanner import gmail as G
    monkeypatch.setattr(G.time, "sleep", lambda s: None)
    calls = {"n": 0}

    class Req:
        def execute(self, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise ssl.SSLEOFError("EOF occurred in violation of protocol")
            return {"ok": 1}
    assert G._call(Req()) == {"ok": 1}


def test_whole_domain_blocked_for_login_companies():
    g = FakeGmail([make_msg("1", "promo@email.microsoft.com", "Deals", 1),
                   make_msg("2", "news@myschool.edu", "Newsletter", 1)])
    sh = FakeSheet()
    sh.append("Review Senders", [{"Sender": "promo@email.microsoft.com", "Decision": "Delete whole domain"},
                                 {"Sender": "news@myschool.edu", "Decision": "Delete whole domain"}])
    backlog.apply_decisions(g, sh)
    assert not any("TRASH" in m["labelIds"] for m in g.msgs.values())
    assert all(r["Status"].startswith("Skipped") for r in sh.read("Review Senders"))
    # plain Delete on the marketing address still works
    sh2 = FakeSheet()
    sh2.append("Review Senders", [{"Sender": "promo@email.microsoft.com", "Decision": "Delete"}])
    backlog.apply_decisions(g, sh2)
    assert "TRASH" in g.msgs["1"]["labelIds"]


def test_whole_domain_auto_keeps_login_senders():
    g = FakeGmail([make_msg("1", "deals@shop.com", "50% off", 1),
                   make_msg("2", "security@shop.com", "Your verification code", 1),
                   make_msg("3", "orders@shop.com", "Order confirmation #123", 1)])
    sh = FakeSheet()
    sh.append("Review Senders", [{"Sender": "deals@shop.com", "Decision": "Delete whole domain"}])
    backlog.apply_decisions(g, sh)
    assert "TRASH" in g.msgs["1"]["labelIds"]
    assert "TRASH" not in g.msgs["2"]["labelIds"] and "TRASH" not in g.msgs["3"]["labelIds"]
    assert g.filters == [("shop.com", ("orders@shop.com", "security@shop.com"))]
    assert "kept 2 login/receipt" in sh.read("Deleted Senders")[0]["Status"]


def test_crash_midway_still_records_progress():
    g = FakeGmail([make_msg("1", "a@x.com", "a", 1), make_msg("2", "b@y.com", "b", 1)])
    real = g.trash
    def boom(ids):
        if "2" in ids:
            raise ConnectionError("dropped")
        return real(ids)
    g.trash = boom
    sh = FakeSheet()
    sh.append("Review Senders", [{"Sender": "a@x.com", "Decision": "Delete"},
                                 {"Sender": "b@y.com", "Decision": "Delete"}])
    with pytest.raises(ConnectionError):
        backlog.apply_decisions(g, sh)
    rows = {r["Sender"]: r for r in sh.read("Review Senders")}
    assert rows["a@x.com"]["Status"].startswith("Done")   # saved despite the crash
    assert rows["b@y.com"]["Status"] == ""                # retried next run
    assert sh.get_state("cleanup_since_digest")["senders"] == 1


def test_tidy_order_and_skipped_can_be_redecided():
    sh = FakeSheet()
    sh.append("Review Senders", [
        {"Sender": "keep@a.com", "Decision": "Keep", "Status": "Kept", "Emails": 900},
        {"Sender": "gone@b.com", "Decision": "Delete", "Status": "Done: trashed 5", "Emails": 5},
        {"Sender": "small@c.com", "Decision": "", "Emails": 3},
        {"Sender": "big@d.com", "Decision": "Later", "Emails": "1,200"},
        {"Sender": "x@microsoft.com", "Decision": "Delete whole domain", "Status": "Skipped: blocked", "Emails": 50},
    ])
    assert backlog.tidy_review(sh) == 1
    assert [r["Sender"] for r in sh.read("Review Senders")] == ["big@d.com", "small@c.com", "x@microsoft.com", "keep@a.com"]
    # choose plain Delete on the skipped row -> processed next run
    g = FakeGmail([make_msg("1", "x@microsoft.com", "promo", 1)])
    row = next(r for r in sh.read("Review Senders") if r["Sender"] == "x@microsoft.com")
    sh.update_cells("Review Senders", row["_row"], {"Decision": "Delete"})
    backlog.apply_decisions(g, sh)
    assert "TRASH" in g.msgs["1"]["labelIds"]
    assert "x@microsoft.com" in [r["Sender"] for r in sh.read("Deleted Senders")]
