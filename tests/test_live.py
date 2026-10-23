import json
from datetime import UTC, datetime

import pytest

from triage import guidelines as gl
from triage import live, server
from triage.config import REPOS
from triage.llm import CallResult
from triage.patrol import Patrol, card_from
from triage.store import Store

EVENT = {
    "type": "edit",
    "server_name": "en.wikipedia.org",
    "server_url": "https://en.wikipedia.org",
    "namespace": 0,
    "bot": False,
    "title": "Wang Dongsheng",
    "comment": "/* Honours */",
    "user": "203.0.113.9",
    "timestamp": 1792787398,
    "length": {"old": 100, "new": 112},
    "revision": {"old": 10, "new": 11},
}


def _ev(**over):
    return {**EVENT, **over}


def test_wants_filters_bots_reverts_other_wikis_and_namespaces():
    assert live.wants(EVENT, "en.wikipedia.org")
    assert not live.wants(_ev(bot=True), "en.wikipedia.org")
    assert not live.wants(_ev(namespace=3), "en.wikipedia.org")
    assert not live.wants(_ev(server_name="de.wikipedia.org"), "en.wikipedia.org")
    assert not live.wants(_ev(comment="Reverted edits by X"), "en.wikipedia.org")
    assert not live.wants(_ev(comment="Undid revision 5 by Y"), "en.wikipedia.org")
    assert not live.wants(_ev(type="new", revision={"new": 11}), "en.wikipedia.org")


def test_item_from_event_marks_ip_editors_and_builds_a_diff():
    item = live.item_from_event(EVENT, "a\nb", "a\nb\n==Honours==", 500)
    assert item["number"] == 11 and item["anon"] is True
    assert item["body"].startswith(
        "Editor: anonymous (IP)\nEdit summary: /* Honours */\nSize change: +12 bytes"
    )
    assert "+==Honours==" in item["body"]
    assert item["url"].endswith("diff=11")
    assert live.item_from_event(_ev(user="Alice"), "a", "b", 500)["anon"] is False


def test_is_provider_and_cost():
    assert live.is_provider("claude-sonnet-5-5", "claude-sonnet-5-5")
    assert live.is_provider("claude-sonnet-5-5-20260901", "claude-sonnet-5-5")
    assert not live.is_provider("mubit-adapter-7", "claude-sonnet-5-5")
    assert not live.is_provider(None, "claude-sonnet-5-5")
    assert live.cost_usd(1_000_000, 100_000) == 2.0 + 1.0


def _call(ts, served, err=None, kind="live"):
    return {
        "ts": ts,
        "kind": kind,
        "model_served": served,
        "latency_ms": 100,
        "prompt_tokens": 1000,
        "completion_tokens": 20,
        "error": err,
    }


def test_summarize_measures_owned_share_and_projects_frontier_cost():
    now = datetime(2026, 10, 24, 1, 0, tzinfo=UTC)
    calls = [
        _call("2026-10-24T00:00:00+00:00", "claude-sonnet-5-5"),
        _call("2026-10-24T00:30:00+00:00", "mubit-adapter-1"),
        _call("2026-10-24T00:40:00+00:00", None, err="HTTP 429"),
        _call("2026-10-24T00:50:00+00:00", "claude-sonnet-5-5", kind="correct"),
    ]
    s = live.summarize(calls, "claude-sonnet-5-5", now)
    assert s["calls"] == 4 and s["answered"] == 3 and s["errors"] == 1 and s["corrections"] == 1
    assert abs(s["owned_share"] - 1 / 3) < 1e-9
    assert (
        s["by_served"]["mubit-adapter-1"]["n"] == 1 and s["by_served"]["claude-sonnet-5-5"]["p50_ms"] == 100
    )
    # The page reads these flags, so the table and the gauge agree on who is "owned".
    assert s["by_served"]["mubit-adapter-1"]["owned"] is True
    assert s["by_served"]["claude-sonnet-5-5"]["owned"] is False
    adapter = live.summarize(
        [_call("2026-10-24T00:00:00+00:00", "claude-sonnet-5-5-adapter")], "claude-sonnet-5-5", now
    )
    assert (
        adapter["by_served"]["claude-sonnet-5-5-adapter"]["owned"] is True and adapter["owned_share"] == 1.0
    )
    # 3 answered calls at $0.0022 each over one hour -> 24x per day
    assert abs(s["frontier_cost_per_day_usd"] - round(3 * 0.0022 * 24, 2)) < 0.01
    assert s["owned_cost_per_day_usd"] is None
    assert live.summarize([], "claude-sonnet-5-5", now)["frontier_cost_per_day_usd"] is None
    # An answered call with no served model is not owned, in the table as on the card.
    unknown = live.summarize([_call("2026-10-24T00:00:00+00:00", None)], "claude-sonnet-5-5", now)
    assert unknown["by_served"]["?"]["owned"] is False and unknown["owned_share"] == 0.0


def _result(labels, extra=None, served="claude-sonnet-5-5"):
    return CallResult(labels, "claude-sonnet-5-5", served, "mreq_1", 50, 900, 15, extra=extra or {})


def test_card_from_reads_verdict_reason_and_owned_flag():
    item = live.item_from_event(EVENT, "a", "a\nspam", 500)
    c = card_from(
        item,
        _result(["revert"], {"reason": "promotional", "confidence": "high"}, "mubit-x"),
        "claude-sonnet-5-5",
    )
    assert c["verdict"] == "revert" and c["reason"] == "promotional" and c["owned"] is True
    assert c["diff"].startswith("@@") and "+spam" in c["diff"]
    e = card_from(
        item,
        CallResult([], "claude-sonnet-5-5", None, None, 10, None, None, error="HTTP 500"),
        "claude-sonnet-5-5",
    )
    assert e["verdict"] is None and e["owned"] is False and e["error"] == "HTTP 500"


def test_verdict_disagreement_writes_guideline_and_resends(monkeypatch, tmp_path):
    monkeypatch.setenv("MUBIT_API_KEY", "mbt_test")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    sent = []

    def fake_classify(c, full_name, labels, issue, guide, model, kind):
        sent.append(guide)
        return _result(["revert"], {"reason": "vandalism", "confidence": "high"})

    monkeypatch.setattr("triage.patrol.classify", fake_classify)
    p = Patrol(REPOS["enwiki"], data_dir=tmp_path)
    item = live.item_from_event(EVENT, "a", "a\nspam", 500)
    p.items[11] = item
    p.cards[11] = card_from(
        item, _result(["keep"], {"reason": "constructive", "confidence": "medium"}), "claude-sonnet-5-5"
    )

    ev = p.verdict(11, "revert")
    assert ev["type"] == "correction" and ev["old"] == "keep" and ev["new"] == "revert"
    assert ev["resent"] == "revert" and ev["agrees"] is True and ev["guidelines_total"] == 1
    assert '(#11) For items like "Wang Dongsheng": add revert; drop keep' in ev["guideline"]
    assert sent == [ev["guideline"] + "\n"]  # the re-send carried the new guideline
    assert p.cards[11]["correction"]["changed"] is True
    rows = p.stats()
    assert rows["corrections"] == 1 and rows["guidelines"] == 1

    # A second click on the same card (double click, second tab) writes and sends nothing.
    with pytest.raises(ValueError):
        p.verdict(11, "keep")
    assert len(sent) == 1 and gl.load("enwiki", set(), tmp_path).count("\n") == 1

    # Agreement records nothing new at Mubit.
    p.cards[11]["correction"] = None
    p.cards[11]["verdict"] = "revert"
    ev2 = p.verdict(11, "revert")
    assert ev2["type"] == "confirmed" and len(sent) == 1


def test_failed_guideline_write_releases_the_claim(monkeypatch, tmp_path):
    monkeypatch.setenv("MUBIT_API_KEY", "mbt_test")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    monkeypatch.setattr("triage.patrol.classify", lambda *a, **k: _result(["revert"]))
    p = Patrol(REPOS["enwiki"], data_dir=tmp_path)
    item = live.item_from_event(EVENT, "a", "a\nspam", 500)
    p.items[11] = item
    p.cards[11] = card_from(item, _result(["keep"]), "claude-sonnet-5-5")

    real, fail_once = gl.add_correction, [OSError("disk full")]

    def flaky(*a, **k):
        if fail_once:
            raise fail_once.pop()
        return real(*a, **k)

    monkeypatch.setattr("triage.patrol.gl.add_correction", flaky)
    with pytest.raises(OSError):
        p.verdict(11, "revert")
    assert p.cards[11]["correction"] is None  # a retry is not refused with 409
    assert p.verdict(11, "revert")["type"] == "correction"


def test_handle_logs_every_live_call_including_errors(monkeypatch, tmp_path):
    monkeypatch.setenv("MUBIT_API_KEY", "mbt_test")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    item = live.item_from_event(EVENT, "a", "a\nspam", 500)
    replies = [
        CallResult(
            ["revert"],
            "claude-sonnet-5-5",
            "claude-sonnet-5-5-20260901",
            "mreq_ok",
            40,
            900,
            15,
            headers={"x-middleware-request-id": "mreq_ok"},
            extra={"reason": "vandalism", "confidence": "high"},
        ),
        CallResult(
            [],
            "claude-sonnet-5-5",
            None,
            "mreq_err",
            30,
            None,
            None,
            headers={"x-middleware-request-id": "mreq_err"},
            error="HTTP 503: overloaded",
        ),
    ]
    monkeypatch.setattr("triage.patrol.fetch_item", lambda http, event, max_chars: item)
    monkeypatch.setattr("triage.patrol.classify", lambda *a, **k: replies.pop(0))
    p = Patrol(REPOS["enwiki"], data_dir=tmp_path)
    store = Store(tmp_path / "calls.sqlite")

    p._handle(store, None, EVENT)
    p._handle(store, None, EVENT)

    rows = store.db.execute(
        "SELECT kind, issue, request_id, headers, model_served, error FROM calls ORDER BY id"
    ).fetchall()
    assert [(r[0], r[1], r[2]) for r in rows] == [("live", 11, "mreq_ok"), ("live", 11, "mreq_err")]
    assert json.loads(rows[0][3]) == {"x-middleware-request-id": "mreq_ok"}
    assert json.loads(rows[1][3]) == {"x-middleware-request-id": "mreq_err"}
    assert rows[0][4] == "claude-sonnet-5-5-20260901" and rows[0][5] is None
    assert rows[1][4] is None and rows[1][5] == "HTTP 503: overloaded"
    s = p.stats(store)
    assert s["calls"] == 2 and s["answered"] == 1 and s["errors"] == 1  # the error is not scored


def test_desk_refuses_non_loopback_hosts_unless_allowed(monkeypatch):
    started = []
    monkeypatch.setattr("triage.server.uvicorn.run", lambda app, **kw: started.append(kw["host"]))
    trusted = []
    monkeypatch.setattr("triage.server.create_app", lambda patrol, hosts: trusted.append(hosts))
    for host in ("127.0.0.1", "localhost", "::1"):
        server.serve(None, host, 8321)
    with pytest.raises(SystemExit):
        server.serve(None, "0.0.0.0", 8321)
    server.serve(None, "0.0.0.0", 8321, allow_remote=True)
    assert started == ["127.0.0.1", "localhost", "::1", "0.0.0.0"]
    assert all("*" not in h for h in trusted[:3]) and trusted[3] == ["*"]


def test_desk_rejects_foreign_host_headers():
    """DNS rebinding: a page on evil.example resolving to 127.0.0.1 must not reach the actions."""
    from fastapi.testclient import TestClient

    class FakePatrol:
        def set_status(self, text):
            return {"text": text}

    c = TestClient(server.create_app(FakePatrol()))
    assert c.post("/status", json={"text": "x"}, headers={"host": "evil.example"}).status_code == 400
    for host in ("127.0.0.1:8321", "localhost:8321", "[::1]:8321"):
        assert c.post("/status", json={"text": "x"}, headers={"host": host}).json() == {"text": "x"}
