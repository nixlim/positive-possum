import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from triage import agreement, cli, diffs, pipeline, steps
from triage import pages as pg
from triage.store import Store

OLD = "Acme is a company.\n== History ==\nFounded in 1990."
NEW = OLD + " It was fined in 2020.<ref>News</ref>"
EXTRACT_OK = {
    "claims_added": ["Acme was fined in 2020"],
    "claims_removed": [],
    "negative_info": "added",
    "tone_shift": -1,
}
PROBE_EXTRACT = {**EXTRACT_OK, "claims_added": ["Acme paid a fine"]}
EMPTY_EXTRACT = {"claims_added": [], "claims_removed": [], "negative_info": "none", "tone_shift": 0}
CLASSIFY_OK = {
    "label": "content",
    "severity": 1,
    "review": True,
    "confidence": "high",
    "rationale": "Adds a fine.",
}
ACCOUNT = {"registration": "2026-10-10T09:00:00Z", "editcount": 12, "groups": ["user"], "page_share": 0.5}


def _rev(revid, **kw):
    rev = {
        "revid": revid,
        "parentid": revid - 1,
        "timestamp": "2026-10-20T10:00:00Z",
        "user": "Zed Example",
        "userid": 99,
        "editor_kind": "registered",
        "comment": "add fine",
        "size": 900,
        "size_delta": 45,
        "minor": False,
        "tags": ["mw-reverted"],
        "title": "Acme",
        "pageid": 7,
        "account": ACCOUNT,
    }
    return rev | kw


class FakeMubit:
    """Answers each request with the next scripted reply for its step: a dict (JSON), a str
    (raw text) or an exception. Served model per step, to check extract_served threading."""

    def __init__(self, replies, served=None):
        self.replies = {k: list(v) for k, v in replies.items()}
        self.served = served or {}
        self.requests = []
        self.messages = SimpleNamespace(with_raw_response=SimpleNamespace(create=self.create))

    def create(self, **req):
        self.requests.append(req)
        step = next(s for s, p in steps.SYSTEMS.items() if p == req["system"])
        reply = self.replies[step].pop(0)
        if isinstance(reply, Exception):
            raise reply
        text = json.dumps(reply) if isinstance(reply, dict) else reply
        msg = anthropic.types.Message.model_validate(
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": self.served.get(step, "adapter-v1"),
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 3},
            }
        )
        headers = httpx.Headers({"x-middleware-request-id": f"mreq_{len(self.requests)}"})
        return SimpleNamespace(headers=headers, parse=lambda: msg)

    def steps_sent(self):
        return [next(s for s, p in steps.SYSTEMS.items() if p == r["system"]) for r in self.requests]

    def contents(self):
        return [r["messages"][0]["content"] for r in self.requests]


def _texts(ids):
    return {i: (NEW if i % 2 else OLD) for i in ids}


def _run(tmp_path, fake, revs, max_calls=20, texts=_texts, kind="pipeline", chained=False):
    store = Store(tmp_path / "calls.sqlite")
    caller = pipeline.Caller(fake, store, kind, max_calls)
    out = pipeline.run(caller, revs, texts, model="claude-sonnet-5-5", chained=chained, log=lambda _m: None)
    return out, store


def _rows(store):
    cols = ("id", "ts", "run_id", "kind", "repo", "issue", "served", "labels", "error", "extra")
    cur = store.db.execute(
        "SELECT id, ts, run_id, kind, repo, issue, model_served, labels, error, extra FROM calls ORDER BY id"
    )
    rows = [dict(zip(cols, r, strict=True)) for r in cur]
    for r in rows:
        r["labels"], r["extra"] = json.loads(r["labels"]), json.loads(r["extra"] or "{}")
    return rows


def test_extract_then_classify_logged_and_chained(tmp_path):
    fake = FakeMubit(
        {steps.EXTRACT: [EXTRACT_OK], steps.CLASSIFY: [CLASSIFY_OK]},
        served={steps.EXTRACT: "adapter-v1", steps.CLASSIFY: "claude-sonnet-5-5"},
    )
    [res], store = _run(tmp_path, fake, [_rev(101)])
    assert fake.steps_sent() == [steps.EXTRACT, steps.CLASSIFY]
    assert res["extract"] == EXTRACT_OK and res["error"] is None
    assert res["classification"] == {
        **CLASSIFY_OK,
        "review_model": True,
        "review_forced": None,
        "source": "model",
    }
    assert res["served"] == {"extract": "adapter-v1", "classify": "claude-sonnet-5-5"}
    ext, cls = _rows(store)
    assert (ext["kind"], ext["repo"], ext["issue"], ext["labels"]) == ("pipeline", steps.EXTRACT, 101, [])
    assert ext["extra"]["measured"] == {**diffs.measure(OLD, NEW), "size_delta": 45}
    assert (cls["repo"], cls["labels"]) == (steps.CLASSIFY, ["content"])
    assert cls["extra"] == {**CLASSIFY_OK, "extract_call_id": ext["id"], "extract_served": "adapter-v1"}
    user = fake.contents()[1]
    assert "account_age_days=10," in user and "refs_added=1" in user and "sections_touched=1\n" in user
    for req in fake.requests:
        assert req["metadata"] == {"user_id": "enwiki#101"}
        for leak in ("Zed Example", "mw-reverted"):
            assert leak not in req["messages"][0]["content"]


def test_forced_review_overrides_the_model_but_not_its_logged_reply(tmp_path):
    calm = {**CLASSIFY_OK, "review": False}
    fake = FakeMubit({steps.EXTRACT: [EXTRACT_OK], steps.CLASSIFY: [calm]})
    [res], store = _run(tmp_path, fake, [_rev(101)], texts=lambda ids: {101: OLD, 100: NEW})  # ref removed
    c = res["classification"]
    assert (c["review"], c["review_model"], c["review_forced"]) == (True, False, "refs_removed")
    ext, cls = _rows(store)
    assert cls["extra"] == {**calm, "extract_call_id": ext["id"], "extract_served": "adapter-v1"}
    assert pipeline.classification(ext["extra"], cls["extra"]) == c  # rebuilt from the log alone


def test_unparseable_reply_retried_once_both_logged(tmp_path):
    fake = FakeMubit({steps.EXTRACT: ["not json", {**EXTRACT_OK, "tone_shift": 7}]})
    [res], store = _run(tmp_path, fake, [_rev(101)])
    assert fake.steps_sent() == [steps.EXTRACT, steps.EXTRACT]
    assert res["error"].startswith("unparseable reply") and res["classification"] is None
    assert [r["error"].split(":")[0] for r in _rows(store)] == ["unparseable reply"] * 2


def test_retry_success_continues_to_classify(tmp_path):
    fake = FakeMubit({steps.EXTRACT: ['{"claims_added": []}', EXTRACT_OK], steps.CLASSIFY: [CLASSIFY_OK]})
    [res], store = _run(tmp_path, fake, [_rev(101)])
    assert fake.steps_sent() == [steps.EXTRACT, steps.EXTRACT, steps.CLASSIFY]
    rows = _rows(store)
    assert rows[2]["extra"]["extract_call_id"] == rows[1]["id"]
    assert res["classification"]["label"] == "content"


def test_transport_error_logged_not_retried(tmp_path):
    fake = FakeMubit({steps.EXTRACT: [RuntimeError("connection reset")]})
    [res], store = _run(tmp_path, fake, [_rev(101)])
    assert len(fake.requests) == 1
    assert res["error"] == "RuntimeError: connection reset"
    assert [r["error"] for r in _rows(store)] == ["RuntimeError: connection reset"]


def test_classify_error_logged(tmp_path):
    fake = FakeMubit({steps.EXTRACT: [EXTRACT_OK], steps.CLASSIFY: [{**CLASSIFY_OK, "label": "spam"}] * 2})
    [res], store = _run(tmp_path, fake, [_rev(101)])
    assert fake.steps_sent() == [steps.EXTRACT, steps.CLASSIFY, steps.CLASSIFY]
    assert res["error"].startswith("unparseable reply") and res["extract"] == EXTRACT_OK
    assert all(r["extra"]["extract_served"] == "adapter-v1" for r in _rows(store)[1:])


def test_tiny_edit_without_claims_skips_classify(tmp_path):
    extracts = [EMPTY_EXTRACT, EMPTY_EXTRACT, EXTRACT_OK]
    fake = FakeMubit({steps.EXTRACT: extracts, steps.CLASSIFY: [CLASSIFY_OK] * 2})
    revs = [_rev(101, size_delta=-19), _rev(103, size_delta=20), _rev(105, size_delta=3)]
    out, store = _run(tmp_path, fake, revs)
    assert (out[0]["classification"]["source"], out[0]["classification"]["label"]) == ("rule", "maintenance")
    assert out[0]["served"]["classify"] is None
    assert out[1]["classification"]["source"] == "model"  # 20 bytes is not tiny
    assert out[2]["classification"]["source"] == "model"  # tiny, but it has a claim
    assert fake.steps_sent() == [steps.EXTRACT, steps.EXTRACT, steps.CLASSIFY, steps.EXTRACT, steps.CLASSIFY]
    assert [r["repo"] for r in _rows(store)].count(steps.CLASSIFY) == 2


def test_bots_never_sent(tmp_path):
    fake = FakeMubit({})
    bot = _rev(101, account={"groups": ["bot", "user"]})
    out, store = _run(tmp_path, fake, [bot, _rev(103, bot=True)])
    assert out == [] and fake.requests == [] and _rows(store) == []


def test_budget_and_resume(tmp_path):
    fake = FakeMubit({steps.EXTRACT: [EXTRACT_OK, EXTRACT_OK], steps.CLASSIFY: [CLASSIFY_OK, CLASSIFY_OK]})
    revs = [_rev(101), _rev(103)]
    out, _ = _run(tmp_path, fake, revs, max_calls=3)
    assert fake.steps_sent() == [steps.EXTRACT, steps.CLASSIFY, steps.EXTRACT]
    assert out[0]["classification"]["label"] == "content"
    assert out[1]["error"] == "budget spent" and out[1]["extract"] == EXTRACT_OK
    fetched = []
    out, store = _run(tmp_path, fake, revs, texts=lambda ids: fetched.append(ids) or _texts(ids))
    assert fake.steps_sent()[3:] == [steps.CLASSIFY]  # only the missing classify is sent
    assert fetched == []  # nothing left to extract, so no wikitext fetched
    assert [r["classification"]["label"] for r in out] == ["content", "content"]
    assert out[0]["served"]["classify"] == "adapter-v1"
    assert len(_rows(store)) == 4
    _run(tmp_path, fake, revs)
    assert len(fake.requests) == 4  # complete: nothing sent


def test_budget_spent_before_first_call(tmp_path):
    fake = FakeMubit({})
    [res], store = _run(tmp_path, fake, [_rev(101)], max_calls=0)
    assert res["error"] == "budget spent" and fake.requests == [] and _rows(store) == []


def test_missing_wikitext_is_an_error_without_a_call(tmp_path):
    fake = FakeMubit({})
    [res], _ = _run(tmp_path, fake, [_rev(101)], texts=lambda ids: {101: NEW})
    assert res["error"].startswith("wikitext unavailable") and fake.requests == []


def test_page_creation_diffs_against_empty(tmp_path):
    fake = FakeMubit({steps.EXTRACT: [EXTRACT_OK], steps.CLASSIFY: [CLASSIFY_OK]})
    _run(tmp_path, fake, [_rev(101, parentid=0)], texts=lambda ids: {101: NEW})
    assert "+Acme is a company." in fake.contents()[0]


def test_probe_reuses_the_baseline_extract_and_chains(tmp_path):
    whitewash = {**CLASSIFY_OK, "label": "whitewash"}
    fake = FakeMubit(
        {steps.EXTRACT: [EXTRACT_OK, PROBE_EXTRACT], steps.CLASSIFY: [CLASSIFY_OK, CLASSIFY_OK, whitewash]}
    )
    _run(tmp_path, fake, [_rev(101)])
    [res], store = _run(tmp_path, fake, [_rev(101)], kind="probe", chained=True)
    assert fake.steps_sent()[2:] == [steps.EXTRACT, steps.CLASSIFY, steps.CLASSIFY]
    sent = fake.contents()
    assert sent[2] == sent[0] and sent[3] == sent[1]  # identical inputs
    assert "Acme paid a fine" in sent[4]  # chained: fed the fresh probe extract
    base_ext, _, p_ext, p_cls, p_chain = rows = _rows(store)
    assert {r["kind"] for r in rows[2:]} == {"probe"} and len({r["run_id"] for r in rows[2:]}) == 1
    assert (
        p_cls["extra"]["extract_call_id"] == base_ext["id"]
        and p_cls["extra"]["extract_served"] == "adapter-v1"
    )
    assert p_chain["extra"]["extract_call_id"] == p_ext["id"]
    assert res["extract"] == PROBE_EXTRACT
    assert (res["classification"]["label"], res["chained"]["label"]) == ("content", "whitewash")
    got = agreement.agreement(rows)
    assert got[steps.EXTRACT]["extract"]["value"] == 0.0
    assert (got[steps.CLASSIFY]["classify"]["value"], got[steps.CLASSIFY]["classify"]["n"]) == (1.0, 1)
    assert (got[steps.CLASSIFY]["chained"]["value"], got[steps.CLASSIFY]["chained"]["n"]) == (0.0, 1)
    [again], _ = _run(tmp_path, fake, [_rev(101)])
    assert len(fake.requests) == 5 and again["extract"] == EXTRACT_OK  # probe rows never resumed from


def test_probe_needs_a_baseline_and_never_counts_as_done(tmp_path):
    fake = FakeMubit(
        {steps.EXTRACT: [PROBE_EXTRACT, EXTRACT_OK, PROBE_EXTRACT], steps.CLASSIFY: [CLASSIFY_OK] * 2}
    )
    [res], _ = _run(tmp_path, fake, [_rev(101)], kind="probe")
    assert res["error"] == "no baseline extract" and fake.steps_sent() == [steps.EXTRACT]
    assert res["extract"] == PROBE_EXTRACT and res["classification"] is None
    _run(tmp_path, fake, [_rev(101)])
    assert fake.steps_sent()[1:] == [steps.EXTRACT, steps.CLASSIFY]  # the probe extract is not reused
    _, store = _run(tmp_path, fake, [_rev(101)], kind="probe")
    rows = _rows(store)
    assert fake.steps_sent()[3:] == [steps.EXTRACT, steps.CLASSIFY]
    assert rows[-1]["extra"]["extract_call_id"] == rows[1]["id"]  # the first non-probe extract


def test_probe_classify_input_is_identical_when_the_account_changed_since(tmp_path):
    fake = FakeMubit({steps.EXTRACT: [EXTRACT_OK, EXTRACT_OK], steps.CLASSIFY: [CLASSIFY_OK] * 2})
    _run(tmp_path, fake, [_rev(101)])
    later = {**ACCOUNT, "editcount": 40, "page_share": 0.1}
    _run(tmp_path, fake, [_rev(101, account=later)], kind="probe")
    sent = fake.contents()
    assert sent[3] == sent[1]  # the baseline's account facts, not the ones read since
    assert "edit_count=12" in sent[3]


def test_brief_appends_closing_line_and_logs(tmp_path):
    fake = FakeMubit({steps.BRIEF: ["## What changed\nA fine was added."]})
    store = Store(tmp_path / "calls.sqlite")
    analysis = {"pageid": 7, "title": "Acme", "risk_score": 0.4}
    res = pipeline.brief(fake, store, analysis, [], model="claude-sonnet-5-5")
    assert res.extra["brief_md"].endswith("\n\n" + steps.CLOSING_LINE)
    assert "output_config" not in fake.requests[0]
    assert fake.requests[0]["metadata"] == {"user_id": "enwiki-page#7"}
    [row] = _rows(store)
    assert (row["kind"], row["repo"], row["issue"], row["labels"]) == ("brief", steps.BRIEF, 7, [])
    assert row["extra"] == res.extra


def _snapshot(revs, title="Acme", pageid=7, users=None):
    """A snapshot in the shape pages.ingest_page builds."""
    keep = [{k: v for k, v in r.items() if k not in ("title", "pageid", "account")} for r in revs]
    return {
        "title": title,
        "pageid": pageid,
        "as_of": "2026-10-24T00:00:00+00:00",
        "days": 90,
        "revisions": [{"reverted": False, "reverting": False, **r} for r in keep],
        "users": users if users is not None else {"Zed Example": ACCOUNT},
        "sections_then": None,
        "sections_now": None,
    }


def test_load_snapshots_reads_what_pages_writes(tmp_path):
    pg.write_snapshot(_snapshot([_rev(101)]), tmp_path)
    pg.write_snapshot(
        _snapshot([_rev(201, user="1.2.3.4", editor_kind="anonymous")], "Beta", 8, {}), tmp_path
    )
    revs = pipeline.load_snapshots(tmp_path)
    assert [(r["revid"], r["title"], r["pageid"]) for r in revs] == [(101, "Acme", 7), (201, "Beta", 8)]
    assert revs[0]["account"] == ACCOUNT and revs[1]["account"] is None
    assert [r["revid"] for r in pipeline.load_snapshots(tmp_path, {"Beta"})] == [201]


def _cli(tmp_path, monkeypatch, fake, store):
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    monkeypatch.setattr(cli, "client", lambda: fake)
    monkeypatch.setattr(cli, "Store", lambda: store)
    monkeypatch.setattr(cli.pg, "fetch_wikitext", lambda _s, ids: _texts(ids))


def test_cli_pipeline_run_from_snapshots(tmp_path, monkeypatch, capsys):
    snaps = tmp_path / "snaps"
    pg.write_snapshot(_snapshot([_rev(101), _rev(103, size_delta=2)]), snaps)
    fake = FakeMubit({steps.EXTRACT: [EXTRACT_OK, EMPTY_EXTRACT], steps.CLASSIFY: [CLASSIFY_OK]})
    _cli(tmp_path, monkeypatch, fake, Store(tmp_path / "calls.sqlite"))
    cli.main(["pipeline", "run", "--snapshots", str(snaps), "--titles", "Acme", "--rate", "0"])
    lines = capsys.readouterr().out.strip().split("\n")
    assert [json.loads(x)["classification"]["source"] for x in lines[:2]] == ["model", "rule"]
    assert lines[2] == "2 revisions; 3 Mubit calls; 0 wiki requests"
    with pytest.raises(SystemExit, match="--chained"):
        cli.main(["pipeline", "run", "--snapshots", str(snaps), "--chained"])


def test_cli_pipeline_brief_from_snapshot_and_log(tmp_path, monkeypatch, capsys):
    fake = FakeMubit(
        {
            steps.EXTRACT: [EXTRACT_OK, EMPTY_EXTRACT],
            steps.CLASSIFY: [{**CLASSIFY_OK, "review": False}],
            steps.BRIEF: ["## What changed\nA fine was added."],
        }
    )
    later = _rev(103, timestamp="2026-10-21T10:00:00Z", size_delta=2)
    _, store = _run(
        tmp_path, fake, [_rev(101), later], texts=lambda ids: {101: OLD, 100: NEW, 103: OLD, 102: OLD}
    )
    pg.write_snapshot(_snapshot([_rev(101), later, _rev(105)]), tmp_path / "pages" / "snapshots")
    _cli(tmp_path, monkeypatch, fake, store)
    cli.main(["pipeline", "brief", "--title", "Acme"])
    assert capsys.readouterr().out.strip().endswith(steps.CLOSING_LINE)
    user = fake.contents()[-1]
    stats = json.loads(user.split("\n")[4])
    assert stats["analytics"]["flagged_30d"] == 1 and "sections_now" not in stats["structure"]
    [flagged] = [json.loads(x) for x in user.split("Flagged edits (most recent first):\n")[1].split("\n")]
    assert flagged["time"] == "2026-10-20T10:00:00Z" and flagged["review_forced"] == "refs_removed"
    with pytest.raises(SystemExit, match="no snapshot"):
        cli.main(["pipeline", "brief", "--title", "Nope"])
