import re
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from triage import guidelines as gl
from triage import live, patrol
from triage import reconcile as rc
from triage.config import REPOS
from triage.llm import CallResult
from triage.patrol import Patrol, card_from
from triage.store import Store

NOW = datetime(2026, 10, 24, 16, 0, tzinfo=UTC)
CALL = {"ts": "2026-10-24T03:12:00+00:00", "issue": 11, "labels": ["revert"]}


def test_outcome_revert_with_lead():
    rev = {"title": "Acme", "tags": ["mw-reverted"], "timestamp": "2026-10-24T03:12:00Z"}
    revert = {"revid": 12, "timestamp": "2026-10-24T03:41:00Z", "user": "Patroller"}
    o = rc.outcome(CALL, rev, revert, NOW)
    assert o["truth"] == "revert" and o["lead_s"] == 29 * 60 and o["reverted_by"] == "Patroller"


def test_outcome_pending_then_keep():
    rev = {"title": "Acme", "tags": [], "timestamp": "2026-10-24T03:12:00Z"}
    assert rc.outcome(CALL, rev, None, datetime(2026, 10, 24, 5, 0, tzinfo=UTC)) is None
    assert rc.outcome(CALL, rev, None, NOW)["truth"] == "keep"
    # A revision missing from the reply is not a known revert: it must not drive a correction.
    assert rc.outcome(CALL, None, None, NOW)["truth"] == "gone"


def test_is_reverting_by_tag_or_summary():
    assert rc.is_reverting({"tags": ["mw-undo"], "comment": "x"})
    assert rc.is_reverting({"tags": [], "comment": "Reverted edits by 1.2.3.4"})
    assert not rc.is_reverting({"tags": ["mw-reverted"], "comment": "add section"})


def test_find_revert_picks_first_reverting_revision_after_ours(monkeypatch):
    history = {
        "query": {
            "pages": [
                {
                    "title": "Acme",
                    "revisions": [
                        {"revid": 11, "timestamp": "2026-10-24T03:12:00Z", "tags": [], "comment": "spam"},
                        {"revid": 12, "timestamp": "2026-10-24T03:20:00Z", "tags": [], "comment": "typo"},
                        {
                            "revid": 13,
                            "timestamp": "2026-10-24T03:41:00Z",
                            "tags": ["mw-rollback"],
                            "user": "P",
                        },
                    ],
                }
            ]
        }
    }
    monkeypatch.setattr(rc, "_api", lambda c, h, p: history)
    assert rc.find_revert(None, "en.wikipedia.org", "Acme", 11, "2026-10-24T03:12:00Z") == {
        "revid": 13,
        "timestamp": "2026-10-24T03:41:00Z",
        "user": "P",
    }


def test_outcome_stats():
    outs = [
        {"verdict": "revert", "truth": "revert", "lead_s": 1740},
        {"verdict": "revert", "truth": "revert", "lead_s": 60},
        # Wikipedia reverted first: a real measurement, so it counts in the median.
        {"verdict": "revert", "truth": "revert", "lead_s": -120},
        {"verdict": "revert", "truth": "keep", "lead_s": None},
        {"verdict": "keep", "truth": "revert", "lead_s": None},
        # No label on a reverted edit is a miss, not a drop from the recall denominator.
        {"verdict": None, "truth": "revert", "lead_s": None},
        {"verdict": "keep", "truth": "keep", "lead_s": None},
        {"verdict": "revert", "truth": "gone", "lead_s": None},
        {"verdict": "keep", "truth": "gone", "lead_s": None},
    ]
    s = rc.outcome_stats(outs)
    assert s["settled"] == 7 and s["gone"] == 2
    assert s["flagged"] == 4 and s["hits"] == 3 and s["false_alarms"] == 1 and s["missed"] == 2
    assert s["precision"] == 0.75 and s["recall"] == 0.6
    assert s["median_lead_min"] == 1.0 and s["ahead"] == 2 and s["behind"] == 1
    assert rc.outcome_stats([])["precision"] is None
    behind = rc.outcome_stats([{"verdict": "revert", "truth": "revert", "lead_s": -300}])
    assert behind["median_lead_min"] == -5.0 and behind["ahead"] == 0 and behind["behind"] == 1


def test_reconcile_writes_outcomes_once(monkeypatch, tmp_path):
    store = Store(tmp_path / "c.sqlite")
    res = CallResult(["revert"], "m", "m", None, 10, 1, 1, extra={})
    store.log_call("live-enwiki", "live", "enwiki", 11, res)
    store.log_call("live-enwiki", "live", "enwiki", 12, res)
    monkeypatch.setattr(
        rc,
        "check_revisions",
        lambda c, h, ids: {
            11: {"title": "Acme", "tags": ["mw-reverted"], "timestamp": "2026-10-24T03:12:00Z"},
            12: {"title": "B", "tags": [], "timestamp": "2026-10-24T03:12:00Z"},
        },
    )
    monkeypatch.setattr(
        rc, "find_revert", lambda *a: {"revid": 13, "timestamp": "2026-10-24T03:41:00Z", "user": "P"}
    )
    soon = datetime.now(UTC)
    written = [o for b in rc.reconcile(store, REPOS["enwiki"], None, soon) for o in b]
    assert [o["issue"] for o in written] == [11] and written[0]["truth"] == "revert"
    assert [c["issue"] for c in store.pending_outcomes("enwiki")] == [12]
    assert [o for b in rc.reconcile(store, REPOS["enwiki"], None, soon) for o in b] == []
    outs = store.outcomes("enwiki")
    assert outs[0]["issue"] == 11 and outs[0]["reverted_by"] == "P"


def test_reconcile_skips_young_calls_off_the_desk_and_writes_each_batch(monkeypatch, tmp_path):
    store = Store(tmp_path / "c.sqlite")
    res = CallResult(["revert"], "m", "m", None, 10, 1, 1, extra={})
    for revid in (11, 12, 13):
        store.log_call("live-enwiki", "live", "enwiki", revid, res)
    monkeypatch.setattr(rc, "CONTENT_BATCH", 1)
    asked = []

    def check(_c, _h, ids):
        asked.extend(ids)
        if len(asked) == 3:
            raise SystemExit("en.wikipedia.org API error: maxlag")
        return {i: {"title": "T", "tags": [], "timestamp": "2026-10-24T03:12:00Z"} for i in ids}

    monkeypatch.setattr(rc, "check_revisions", check)
    cfg = REPOS["enwiki"]
    # Young: only the card still on the desk is asked about, and it is not settled yet.
    assert list(rc.reconcile(store, cfg, None, datetime.now(UTC), desk={12})) == [[]]
    assert asked == [12]
    asked.clear()
    # Old enough to settle: every call is asked; the batches before the API error are kept.
    later = datetime.now(UTC) + timedelta(hours=rc.SETTLE_HOURS + 1)
    with pytest.raises(SystemExit):
        for _ in rc.reconcile(store, cfg, None, later, desk=set()):
            pass
    assert len(asked) == 3
    assert len(store.outcomes("enwiki")) == 2 and len(store.pending_outcomes("enwiki")) == 1


EVENT = {
    "type": "edit",
    "server_name": "en.wikipedia.org",
    "server_url": "https://en.wikipedia.org",
    "namespace": 0,
    "bot": False,
    "title": "Acme",
    "comment": "",
    "user": "203.0.113.9",
    "timestamp": 1792787398,
    "length": {"old": 1, "new": 2},
    "revision": {"old": 1, "new": 11},
}


def _desk(monkeypatch, tmp_path, verdicts):
    """A Patrol with cards on the desk, a stub Mubit that records re-sends, and captured broadcasts."""
    monkeypatch.setenv("MUBIT_API_KEY", "mbt_test")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    sent = []

    def fake_classify(*a, **_k):
        sent.append(a[3]["number"])
        return CallResult(["revert"], "m", "m", "mreq", 10, 1, 1, extra={})

    monkeypatch.setattr("triage.patrol.classify", fake_classify)
    p = Patrol(REPOS["enwiki"], data_dir=tmp_path)
    events = []
    monkeypatch.setattr(p, "_broadcast", lambda kind, data: events.append((kind, data)))
    for revid, v in verdicts.items():
        ev = {**EVENT, "title": f"T{revid}", "revision": {"old": 1, "new": revid}}
        item = live.item_from_event(ev, "a", "a\nb", 500)
        p.items[revid] = item
        p.cards[revid] = card_from(item, CallResult([v], "m", "m", None, 10, 1, 1, extra={}), "m")
    return p, sent, events


def _out(issue, verdict, truth):
    return {
        "issue": issue,
        "verdict": verdict,
        "truth": truth,
        "reverted_at": None,
        "reverted_by": None,
        "lead_s": None,
    }


def test_apply_outcomes_corrects_only_open_disagreements(monkeypatch, tmp_path):
    p, sent, events = _desk(
        monkeypatch, tmp_path, {11: "revert", 12: "keep", 13: "revert", 14: "keep", 15: "keep"}
    )
    p.cards[15]["correction"] = {"verdict": "keep", "changed": False, "source": "patroller"}
    p.apply_outcomes(
        [
            _out(11, "revert", "revert"),  # agreement: card re-broadcast, nothing sent
            _out(12, "keep", "revert"),  # disagreement: one guideline line, one re-send
            _out(15, "keep", "revert"),  # a patroller already corrected it: not twice
            _out(99, "keep", "revert"),  # left the desk: skipped
            _out(13, "revert", "gone"),  # revision not returned: no known truth, no correction
            _out(14, "keep", "keep"),
        ]
    )
    assert sent == [12]
    assert gl.load("enwiki", set(), tmp_path).count("\n") == 1
    assert p.cards[12]["correction"]["source"] == "wikipedia" and p.cards[12]["correction"]["resent"]
    assert p.cards[15]["correction"]["source"] == "patroller"
    assert p.cards[13]["correction"] is None and p.cards[13]["outcome"]["truth"] == "gone"
    assert all(p.cards[i]["outcome"] for i in (11, 12, 13, 14, 15))
    assert [d["number"] for k, d in events if k == "card"] == [11, 12, 15, 13, 14]
    assert [d["source"] for k, d in events if k == "learning"] == ["wikipedia"]
    assert events[-1][0] == "stats"


def test_apply_outcomes_survives_a_failing_correction(monkeypatch, tmp_path):
    p, sent, events = _desk(monkeypatch, tmp_path, {11: "keep", 12: "keep"})

    def disk_full(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr("triage.patrol.gl.add_correction", disk_full)
    p.apply_outcomes([_out(11, "keep", "revert"), _out(12, "keep", "keep")])
    assert sent == [] and p.cards[11]["correction"] is None  # claim released for a retry
    assert p.cards[11]["outcome"]["truth"] == "revert" and p.cards[12]["outcome"]["truth"] == "keep"
    kinds = [k for k, _ in events]
    assert kinds == ["log", "card", "stats"] and "OSError" in events[0][1]["text"]


def test_reconciler_survives_a_wikipedia_api_error(monkeypatch, tmp_path):
    p, _, events = _desk(monkeypatch, tmp_path, {11: "revert"})  # on the desk, so it is queried
    Store(tmp_path / "calls.sqlite").log_call(
        "live-enwiki", "live", "enwiki", 11, CallResult(["revert"], "m", "m", None, 10, 1, 1, extra={})
    )
    monkeypatch.setattr(patrol, "RECONCILE_EVERY_S", 0)
    calls = []

    def api(*_a):
        calls.append(1)
        if len(calls) == 1:
            raise SystemExit("en.wikipedia.org API error: maxlag")  # what wiki._api raises
        p.stopping.set()
        return {"query": {"pages": []}}

    monkeypatch.setattr(rc, "_api", api)
    p._reconciler()  # returns once stopping is set; a dead thread would have stopped at call 1
    assert len(calls) == 2
    assert events[0][0] == "log" and "SystemExit" in events[0][1]["text"]
    assert [o["issue"] for o in Store(tmp_path / "calls.sqlite").outcomes("enwiki")] == [11]


def test_patrol_page_script_parses(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("node is not installed")
    html = (Path(patrol.__file__).parent / "static" / "patrol.html").read_text()
    scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
    assert scripts
    js = tmp_path / "patrol.js"
    js.write_text("\n".join(scripts))
    r = subprocess.run([node, "--check", str(js)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
