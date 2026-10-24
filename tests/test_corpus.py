from datetime import UTC, date, datetime

import httpx
import pytest

from triage import corpus as cp
from triage import wiki


def _rc(revid, pageid=1, user="A", ts="2026-10-20T10:00:00Z", tags=(), comment="c", **kw):
    return {
        "revid": revid,
        "pageid": pageid,
        "old_revid": revid - 1,
        "user": user,
        "timestamp": ts,
        "tags": list(tags),
        "comment": comment,
        **kw,
    }


def test_sample_windows_cover_every_hour_and_hold_out_the_latest_third():
    anchor = date(2026, 10, 24)
    wins = cp.sample_windows(anchor, 24)
    starts = [cp.ts(w["start"]) for w in wins]
    assert starts == sorted(starts)
    assert {s.hour for s in starts} == set(range(24))
    assert all((cp.ts(w["end"]) - cp.ts(w["start"])).total_seconds() == 3600 for w in wins)
    days = {(datetime.combine(anchor, datetime.min.time(), UTC) - s).days for s in starts}
    assert min(days) >= 3 and max(days) <= 27
    assert [w["test"] for w in wins] == [False] * 16 + [True] * 8


def test_day_chunks_cut_at_midnight():
    chunks = cp.day_chunks(cp.ts("2026-10-01T10:00:00Z"), cp.ts("2026-10-03T05:00:00Z"))
    assert chunks == [
        ("2026-10-01T10:00:00Z", "2026-10-02T00:00:00Z"),
        ("2026-10-02T00:00:00Z", "2026-10-03T00:00:00Z"),
        ("2026-10-03T00:00:00Z", "2026-10-03T05:00:00Z"),
    ]


def test_is_human_content():
    assert cp.is_human_content(_rc(5))
    assert not cp.is_human_content(_rc(5, bot=True))
    assert not cp.is_human_content(_rc(5, old_revid=0))
    assert not cp.is_human_content(_rc(5, tags=["mw-undo"]))
    assert not cp.is_human_content(_rc(5, comment="Reverted edits by X"))


def test_link_reverts_prefers_the_revert_citing_the_revision():
    edit = _rc(100, user="A", tags=["mw-reverted"])
    reverters = [
        _rc(90, comment="Undid revision 100 by A"),  # earlier: cannot have undone it
        _rc(105, comment="Reverted edits by B", ts="2026-10-20T10:05:00Z"),
        _rc(110, comment="Undid revision 100 by A", ts="2026-10-20T10:10:00Z", user="P", bot=True),
        _rc(120, pageid=2, comment="Undid revision 100"),  # another page
    ]
    links = cp.link_reverts([edit], reverters)
    assert links[100]["reverter_revid"] == 110
    assert links[100]["link"] == "revid"
    assert links[100]["delay_s"] == 600
    assert links[100]["reverter_bot"] is True


def test_link_reverts_falls_back_to_the_named_user_then_the_next_revert():
    named = cp.link_reverts([_rc(100, user="A")], [_rc(105, comment="rv B"), _rc(106, comment="rv A")])
    assert (named[100]["reverter_revid"], named[100]["link"]) == (106, "user")
    nxt = cp.link_reverts([_rc(100, user="A")], [_rc(105, comment="rv"), _rc(106, comment="rv")])
    assert (nxt[100]["reverter_revid"], nxt[100]["link"]) == (105, "next")
    assert cp.link_reverts([_rc(100)], [_rc(99)]) == {}


def test_choose_sample_takes_reverted_edits_and_fills_with_kept_ones():
    edits = [_rc(i, tags=["mw-reverted"]) for i in range(2, 12)]
    edits += [_rc(i) for i in range(100, 200)]
    edits += [_rc(500, bot=True), _rc(501, tags=["mw-rollback"])]  # never sampled
    sample, info = cp.choose_sample(edits, 20, seed=1)
    assert info == {"population": {"revert": 10, "keep": 100}, "sampled": {"revert": 10, "keep": 10}}
    assert len({e["revid"] for e in sample}) == 20
    _, capped = cp.choose_sample(edits, 8)
    assert capped["sampled"] == {"revert": 4, "keep": 4}


def _session(tmp_path, handler, budget=100, per_min=6000):
    s = cp.Session(tmp_path, budget, per_min)
    s.client = httpx.Client(transport=httpx.MockTransport(handler), event_hooks={"request": [s._on_request]})
    return s


def test_session_caches_on_disk_and_maps_missing_revisions_to_none(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(404, json={"errorKey": "rest-compare-nonexistent"})

    s = _session(tmp_path, handler)
    url = cp.COMPARE_URL.format(old=1, new=2)
    assert s.rest(url) is None
    assert s.rest(url) is None
    assert len(calls) == 1 and s.hits == 1
    again = _session(tmp_path, handler)  # a re-run reads the same cache
    assert again.rest(url) is None and len(calls) == 1


def test_session_counts_requests_and_stops_at_the_budget(tmp_path):
    s = _session(tmp_path, lambda r: httpx.Response(200, json={"ok": 1}), budget=2)
    s.rest("https://en.wikipedia.org/a")
    s.rest("https://en.wikipedia.org/b")
    assert s.sent == {"en.wikipedia.org": 2}
    with pytest.raises(SystemExit):
        s.rest("https://en.wikipedia.org/c")


def test_session_paces_requests_across_threads(tmp_path, monkeypatch):
    slept = []
    monkeypatch.setattr(cp.time, "sleep", slept.append)
    s = _session(tmp_path, lambda r: httpx.Response(200, json={}), per_min=60)  # one a second
    cp.parallel(s.rest, [f"https://en.wikipedia.org/{i}" for i in range(4)])
    assert sorted(round(x) for x in slept) == [0, 1, 2, 3]


def test_retry_wait():
    def resp(status, **headers):
        return httpx.Response(status, headers=headers)

    assert wiki.retry_wait(resp(200), 0) is None
    assert wiki.retry_wait(resp(404), 0) is None
    assert wiki.retry_wait(resp(429, **{"Retry-After": "5"}), 0) == 5.0
    assert wiki.retry_wait(resp(503), 2) == 4.0
    assert wiki.retry_wait(resp(429, **{"Retry-After": "999"}), 0) == wiki.MAX_RETRY_WAIT_S
    assert wiki.retry_wait(resp(200, **{"MediaWiki-API-Error": "maxlag", "Retry-After": "2"}), 0) == 2.0


def test_send_retries_rate_limits_then_gives_up(monkeypatch):
    slept = []
    monkeypatch.setattr(wiki.time, "sleep", slept.append)
    replies = iter([httpx.Response(429, headers={"Retry-After": "3"}), httpx.Response(200, json={})])
    client = httpx.Client(transport=httpx.MockTransport(lambda r: next(replies)))
    assert wiki._send(client, "GET", "https://en.wikipedia.org/x").status_code == 200
    assert slept == [3.0]
    always = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(429)))
    assert wiki._send(always, "GET", "https://en.wikipedia.org/x").status_code == 429
    assert len(slept) == 1 + wiki.MAX_ATTEMPTS - 1
