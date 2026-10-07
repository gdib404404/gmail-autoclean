import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from emailscanner import sweep
from emailscanner.config import Config
from fakes import FakeGmail, FakeSheet, make_msg

NOW = int(time.time() * 1000)
DAY = 86_400_000


def _training_sheet(n=12):
    sh = FakeSheet()
    arch, rev = [], []
    for i in range(n):
        arch.append({"Sender": f"promo{i}@shop{i}.com", "Decision": "Delete", "Status": "Done: trashed 40",
                     "Emails": 40 + i, "Unread": 40 + i, "Unsubscribe": "one-click",
                     "Sample subjects": "50% off everything | Flash sale ends tonight | New arrivals"})
        rev.append({"Sender": f"friend{i}@gmail.com", "Decision": "Keep", "Status": "Kept",
                    "Emails": 5, "Unread": 0, "You replied?": "Yes", "Unsubscribe": "none",
                    "Sample subjects": "Re: dinner | hey | Re: weekend"})
    sh.append("Deleted Senders", arch)
    sh.append("Review Senders", rev)
    return sh


def test_sweep_end_to_end():
    sh = _training_sheet()
    sh.append("Always Important", [{"Sender or domain": "mom@gmail.com"}])
    ou = "<https://x/u>"
    msgs = [
        make_msg("1", "Deals <deals@newstore.com>", "40% off this weekend only", NOW - DAY, unsub=ou,
                 unsub_post="List-Unsubscribe=One-Click", labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS")),
        make_msg("2", "Deals <deals@newstore.com>", "Flash sale: new arrivals", NOW - 2 * DAY, unsub=ou,
                 unsub_post="List-Unsubscribe=One-Click", labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS")),
        make_msg("3", "Amazon <auto-confirm@amazon.com>", "Your Amazon.com order #112-555 has shipped", NOW - DAY),
        make_msg("4", "Amazon <store-news@amazon.com>", "Deals on headphones: 30% off", NOW - DAY, unsub=ou,
                 unsub_post="List-Unsubscribe=One-Click", labels=("INBOX", "CATEGORY_PROMOTIONS")),
        make_msg("5", "Sam <sam.r@gmail.com>", "yo you coming saturday", NOW - DAY),
        make_msg("6", "Mom <mom@gmail.com>", "call me", NOW - DAY, unsub=ou),
        make_msg("7", "Acct <no-reply@accounts.site.com>", "Your verification code is 123456", NOW - 40 * DAY),
        make_msg("8", "promo3@shop3.com", "sale", NOW - DAY, unsub=ou),
        make_msg("9", "Greenhouse <no-reply@greenhouse.io>", "Thank you for applying to Acme", NOW - DAY, unsub=ou),
        make_msg("10", "Newsletter <hi@blog.io>", "This week in rust", NOW - DAY, unsub="<mailto:u@blog.io>",
                 labels=("INBOX",)),
    ]
    g = FakeGmail(msgs)
    checked = []

    def fake_body_check(gmail, ms, cfg):
        checked.extend(m["id"] for m in ms)
        return [{"tier": "junk", "junk_certainty": 97, "reason": "promo"} for _ in ms]
    import pytest
    mp = pytest.MonkeyPatch()
    mp.setattr(sweep, "body_check", fake_body_check)
    r = sweep.run_sweep(g, sh)
    mp.undo()
    rows = {(x["Sender"], x["Action"]): x for x in sh.read("Sweep Preview")}
    act = {s: a for (s, a) in rows}
    assert act["deals@newstore.com"] == "Delete"
    assert set(checked) == {"1", "2", "4"}                     # only junk candidates get their body read
    assert act["store-news@amazon.com"] == "Delete"             # Amazon promo goes, Amazon order stays
    assert act["auto-confirm@amazon.com"] == "Keep"
    assert act["sam.r@gmail.com"] == "Keep"
    assert act["mom@gmail.com"] == "Keep"
    assert act["no-reply@accounts.site.com"] == "Delete"      # 40-day-old code
    assert act["promo3@shop3.com"] == "Delete"                # already-deleted sender leaking
    assert act["no-reply@greenhouse.io"] == "Keep"
    assert r["model"]["trained"] and r["model"]["cv_accuracy"] >= 0.9
    summary = {x["Item"]: x["Value"] for x in sh.read("Sweep Summary")}
    assert "Would delete" in summary and "THINGS THAT COULD CAUSE MISTAKES" in summary
    # nothing in Gmail changed
    assert not any("TRASH" in m["labelIds"] for m in g.msgs.values()) and not g.filters and not g.sent


def test_sweep_without_enough_decisions_uses_rules():
    sh = FakeSheet()
    g = FakeGmail([make_msg("1", "x@list.com", "Weekly digest", NOW - DAY, unsub="<https://u>",
                            unsub_post="List-Unsubscribe=One-Click", labels=("INBOX", "UNREAD"))])
    r = sweep.run_sweep(g, sh)
    assert not r["model"]["trained"]
    assert sh.read("Sweep Preview")[0]["Action"] in ("Delete", "Review")


def test_candidates_without_body_check_go_to_review():
    sh = _training_sheet()
    g = FakeGmail([make_msg("1", "Deals <deals@newstore.com>", "40% off this weekend only", NOW - DAY,
                            unsub="<https://x/u>", unsub_post="List-Unsubscribe=One-Click",
                            labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS"))])
    sweep.run_sweep(g, sh)   # no API key in tests -> no body check
    assert sh.read("Sweep Preview")[0]["Action"] == "Review"


def test_body_check_saying_important_blocks_delete(monkeypatch):
    sh = _training_sheet()
    g = FakeGmail([make_msg(str(i), "Deals <deals@newstore.com>", f"{i*10}% off, sale", NOW - i * DAY,
                            unsub="<https://x/u>", unsub_post="List-Unsubscribe=One-Click",
                            labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS")) for i in range(1, 9)])
    # 5 get checked; one looks important -> none of this sender's mail is auto-deleted
    verdicts = iter([{"tier": "junk", "junk_certainty": 97}] * 4 + [{"tier": "low", "junk_certainty": 0,
                                                                     "reason": "mentions your account"}])
    monkeypatch.setattr(sweep, "body_check", lambda gm, ms, cfg: [next(verdicts) for _ in ms])
    r = sweep.run_sweep(g, sh)
    acts = Counter(x["Action"] for x in sh.read("Sweep Preview") for _ in range(int(x["Emails"])))
    assert acts["Review"] >= 4 and acts["Delete"] == 4


def test_clean_mode_trashes_labels_and_recovery_teaches(monkeypatch):
    sh = _training_sheet()
    g = FakeGmail([make_msg("1", "Deals <deals@newstore.com>", "40% off this weekend only", NOW - DAY,
                            unsub="<https://x/u>", unsub_post="List-Unsubscribe=One-Click",
                            labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS")),
                   make_msg("2", "Sam <sam.r@gmail.com>", "yo", NOW - DAY)])
    monkeypatch.setattr(sweep, "body_check", lambda gm, ms, cfg: [{"tier": "junk", "junk_certainty": 98}] * len(ms))
    r = sweep.run_sweep(g, sh, clean=True)
    assert r["cleaned"] == 1
    assert "TRASH" in g.msgs["1"]["labelIds"] and g.labels["Auto-cleaned"] in g.msgs["1"]["labelIds"]
    assert "TRASH" not in g.msgs["2"]["labelIds"]
    pending = sh.get_state("auto_cleaned_since_digest")
    assert pending["count"] == 1 and "rfc822msgid" in pending["items"][0]["link"] or "#search/" in pending["items"][0]["link"]
    # you pull it back out of Trash
    g.msgs["1"]["labelIds"].remove("TRASH")
    assert sweep.detect_recoveries(g, sh) == ["deals@newstore.com"]
    assert g.labels["Auto-cleaned"] not in g.msgs["1"]["labelIds"]
    # next sweep never auto-deletes that sender again
    r2 = sweep.run_sweep(g, sh, clean=True)
    assert r2["cleaned"] == 0 and "TRASH" not in g.msgs["1"]["labelIds"]


def test_quiet_senders_deleted_except_receipts_school_and_people():
    sh = FakeSheet()
    yr = 365 * DAY
    ou = "<https://x/u>"
    msgs = [
        make_msg("1", "Admissions <admissions@bigstate.edu>", "Complete your application today!", NOW - 6 * yr, unsub=ou),
        make_msg("2", "Admissions <admissions@bigstate.edu>", "Visit campus this fall", NOW - 7 * yr, unsub=ou),
        make_msg("3", "School <admissions@myschool.edu>", "Welcome to campus!", NOW - 6 * yr, unsub=ou),
        make_msg("4", "Alumni <news@alumni.myschool.edu>", "Homecoming", NOW - 5 * yr, unsub=ou),
        make_msg("5", "Shop <orders@oldshop.com>", "Your order #551 has shipped", NOW - 5 * yr, unsub=ou),
        make_msg("6", "Old Friend <buddy@gmail.com>", "sup", NOW - 6 * yr),
        make_msg("7", "Brochures <info@college.edu>", "Our viewbook", NOW - 5 * yr, unsub=ou),
        # domain still active through another address -> not quiet
        make_msg("8", "Store <promo@active.com>", "Old promo", NOW - 5 * yr, unsub=ou),
        make_msg("9", "Store <news@active.com>", "Fresh news", NOW - DAY, unsub=ou),
    ]
    g = FakeGmail(msgs)
    g.msgs["7"]["_attach"] = True
    sweep.run_sweep(g, sh, cfg=Config(dormant_years=3, never_auto_delete=["myschool.edu"]))
    act = {x["Sender"]: x["Action"] for x in sh.read("Sweep Preview")}
    assert act["admissions@bigstate.edu"] == "Delete"
    assert act["admissions@myschool.edu"] == "Keep" and act["news@alumni.myschool.edu"] == "Keep"
    assert act["orders@oldshop.com"] == "Keep"
    assert act["buddy@gmail.com"] == "Keep"
    assert act["info@college.edu"] == "Review"
    assert act["promo@active.com"] == "Review"                 # its domain emailed recently, so not "quiet"
    summary = {x["Item"]: x["Value"] for x in sh.read("Sweep Summary")}
    assert summary["Deleted only because the sender went quiet for years"] == "2"


def test_quiet_rule_off_when_zero():
    sh = FakeSheet()
    g = FakeGmail([make_msg("1", "a@bigstate.edu", "Visit campus", NOW - 6 * 365 * DAY, unsub="<https://x>")])
    sweep.run_sweep(g, sh, cfg=Config(dormant_years=0))
    assert sh.read("Sweep Preview")[0]["Action"] != "Delete"


def test_model_leaning_junk_needs_claude_95():
    base = {"now_ms": NOW, "deleted": set(), "deleted_domains": set(), "kept": set(), "protected": set(),
            "replied": set(), "my_threads": set(), "sender_unread": {"a@list.com": 1.0},
            "sender_p": {"a@list.com": 0.7}, "min_certainty": 90}
    from emailscanner.senders import parse_message
    m = parse_message(make_msg("1", "a@list.com", "Big sale this weekend", NOW - DAY, unsub="<https://u>",
                               labels=("INBOX", "UNREAD", "CATEGORY_PROMOTIONS")))
    assert sweep.decide(m, base)[0] == "candidate"
    assert sweep.decide(m, base, {"tier": "junk", "junk_certainty": 92})[0] == "review"
    assert sweep.decide(m, base, {"tier": "junk", "junk_certainty": 96})[0] == "delete"
    base["sender_p"]["a@list.com"] = 0.5
    assert sweep.decide(m, base)[0] == "review"      # model not leaning junk -> Claude never asked


def test_replied_and_important_senders_count_as_keep_examples():
    review = [{"Sender": "boss@work.com", "You replied?": "Yes"},
              {"Sender": "news@myschool.edu"},
              {"Sender": "nothing@x.com"},
              {"Sender": "gone@y.com", "Decision": "Delete", "You replied?": "Yes"}]
    archive = [{"Sender": "promo@z.com", "Status": "Done: trashed 3"}]
    rows, labels = sweep.training_rows(review, archive, {"myschool.edu"})
    got = {r["Sender"]: y for r, y in zip(rows, labels)}
    assert got == {"promo@z.com": 1, "gone@y.com": 1, "boss@work.com": 0, "news@myschool.edu": 0}
