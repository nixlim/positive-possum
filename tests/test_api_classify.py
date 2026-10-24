"""POST /api/page/classify and its worker (PIPELINE.md sections 7.4 and 9)."""

import httpx
import pytest
from api_fakes import (
    EMPTY_EXTRACT,
    EXTRACT_OK,
    JANE,
    JOHN,
    Mubit,
    api_error,
    drain,
    log_row,
    page,
    post,
    rate_limited,
    service,
    snapshot,
    wiki_session,
    wikitext_of,
)
from test_pages import _page_route
from test_pipeline import _rows

from triage import pages as pg
from triage import steps
from triage.store import Store

E, C = steps.EXTRACT, steps.CLASSIFY
COUNTS = ("classify_pending", "classify_errored", "classify_open", "classify_stopped", "risk_partial")


def _state(client, title=JANE):
    return tuple(page(client, title)[k] for k in COUNTS)


def _classify_both(client):
    for title in (JANE, JOHN):
        assert post(client, "/api/page/classify", title).status_code == 202


def test_two_posts_in_a_row_queue_each_edit_once_and_rows_carry_kind_page(tmp_path, monkeypatch):
    svc, client, _, mubit = service(tmp_path, monkeypatch)
    first = post(client, "/api/page/classify")
    assert (first.status_code, first.json()) == (
        202,
        {"queued": 2, "already": 0, "errored": 0, "pending": 0, "skipped": 0, "remaining": 300},
    )
    assert post(client, "/api/page/classify").json() == {
        "queued": 0,
        "already": 0,
        "errored": 0,
        "pending": 2,
        "skipped": 0,
        "remaining": 300,
    }
    assert _state(client) == (2, 0, 0, None, True)
    drain(svc)
    assert mubit.sent == [E, C, E, C] and _state(client) == (0, 0, 0, None, False)
    assert {r["kind"] for r in _rows(Store(svc.calls_path))} == {"page"}
    assert [r["issue"] for r in _rows(Store(svc.calls_path))] == [13, 13, 11, 11]  # newest first
    again = post(client, "/api/page/classify").json()
    assert (again["queued"], again["already"]) == (0, 2)


def test_the_spendable_budget_reserves_four_calls_per_pending_job_on_every_page(tmp_path, monkeypatch):
    svc, client, _, mubit = service(tmp_path, monkeypatch, pages=(JANE, JOHN), budget=14, live_reserve=2)
    assert post(client, "/api/page/classify", JANE).json()["queued"] == 2  # 12 spendable: 3 jobs
    second = post(client, "/api/page/classify", JOHN).json()  # 12 - 4·2 = 4: one job
    assert (second["queued"], second["skipped"], second["remaining"]) == (1, 1, 14)
    third = post(client, "/api/page/classify", JOHN)
    assert (third.status_code, third.json()) == (429, {"reason": "budget"})
    assert mubit.sent == [] and len(svc.queue.pending) == 3


def test_classify_and_brief_stop_at_the_mubit_live_reserve(tmp_path, monkeypatch):
    svc, client, _, mubit = service(tmp_path, monkeypatch, budget=63, live_reserve=60)
    r = post(client, "/api/page/classify")
    assert (r.status_code, r.json()) == (429, {"reason": "budget"})  # 3 spendable: under one job
    assert post(client, "/api/page/brief").status_code == 200
    (svc.brief_dir / "5.json").unlink()
    assert svc.limiter.take() and svc.limiter.take()
    r = post(client, "/api/page/brief")
    assert (r.status_code, r.json()) == (429, {"reason": "budget"})
    assert mubit.sent == [steps.BRIEF] and svc.limiter.remaining == 60
    status = client.get("/api/mubit/status").json()["mubit"]
    assert status == {"spent": 3, "remaining": 60}


def test_a_budget_spent_mid_queue_drops_the_leftovers(tmp_path, monkeypatch):
    svc, client, _, mubit = service(tmp_path, monkeypatch, pages=(JANE, JOHN), budget=18, live_reserve=2)
    _classify_both(client)
    assert len(svc.queue.pending) == 4
    for _ in range(13):  # the live feed spends meanwhile: 3 calls stay spendable
        svc.limiter.take(use_reserve=True)
    drain(svc)
    assert mubit.sent == [E, C, E]  # 13 answered, 11 extracted, its classify refused at the reserve
    assert _state(client, JANE) == (0, 0, 1, "budget", True)
    assert _state(client, JOHN) == (0, 0, 2, "budget", True)
    assert svc.queue.jobs.empty() and svc.queue.pending == {}
    assert [r["issue"] for r in _rows(Store(svc.calls_path)) if r["repo"] == E] == [13, 11]  # extract kept


def test_the_worker_checks_the_reserve_before_a_retry(tmp_path, monkeypatch):
    mubit = Mubit({E: ["not json"]})
    svc, client, _, _ = service(tmp_path, monkeypatch, budget=8, live_reserve=4, mubit=mubit)
    assert post(client, "/api/page/classify").json()["queued"] == 1
    for _ in range(3):
        svc.limiter.take(use_reserve=True)
    drain(svc)
    assert mubit.sent == [E] and len(_rows(Store(svc.calls_path))) == 1  # the retry was never sent
    assert _state(client) == (0, 0, 2, "budget", True)  # one unparseable reply is not final


def test_a_gateway_429_drops_the_leftovers_and_a_post_resets_only_its_page(tmp_path, monkeypatch):
    mubit = Mubit({E: [rate_limited()]})
    svc, client, _, _ = service(tmp_path, monkeypatch, pages=(JANE, JOHN), mubit=mubit)
    _classify_both(client)
    drain(svc)
    assert mubit.sent == [E] and _rows(Store(svc.calls_path))[0]["error"].startswith("HTTP 429")
    assert _state(client, JANE) == (0, 0, 2, "budget", True)
    assert _state(client, JOHN) == (0, 0, 2, "budget", True)
    again = post(client, "/api/page/classify", JANE).json()
    assert again["queued"] == 2  # the gateway-error extract is queued again
    assert (
        page(client, JANE)["classify_stopped"] is None and page(client, JOHN)["classify_stopped"] == "budget"
    )


def test_a_classify_gateway_error_is_queued_again_and_an_answered_extract_sends_only_classify(
    tmp_path, monkeypatch
):
    mubit = Mubit({C: [RuntimeError("connection reset")]})
    svc, client, _, _ = service(tmp_path, monkeypatch, mubit=mubit)
    post(client, "/api/page/classify")
    drain(svc)
    assert mubit.sent == [E, C, E, C]  # a gateway error is not retried and ends only its edit
    assert _state(client) == (0, 0, 1, None, True)
    assert post(client, "/api/page/classify").json() | {"remaining": 0} == {
        "queued": 1,
        "already": 1,
        "errored": 0,
        "pending": 0,
        "skipped": 0,
        "remaining": 0,
    }
    drain(svc)
    assert mubit.sent[4:] == [C] and _state(client) == (0, 0, 0, None, False)


@pytest.mark.parametrize("step", [E, C])
def test_two_unparseable_replies_are_final_and_never_re_sent(tmp_path, monkeypatch, step):
    mubit = Mubit({step: ["not json", "still not json"]})
    svc, client, _, _ = service(tmp_path, monkeypatch, mubit=mubit)
    post(client, "/api/page/classify")
    drain(svc)
    first = [E, E] if step == E else [E, C, C]  # an errored extract never gets a classify call
    assert mubit.sent == [*first, E, C]
    assert _state(client) == (0, 1, 0, None, True)
    again = post(client, "/api/page/classify").json()
    assert (again["queued"], again["already"], again["errored"]) == (0, 1, 1)


def test_rule_labelled_edits_are_never_re_sent(tmp_path, monkeypatch):
    snap = snapshot()
    snap["revisions"][0]["size_delta"] = 5  # revision 11: a tiny edit
    pg.write_snapshot(snap, tmp_path / "pages" / "snapshots")
    mubit = Mubit({E: [EXTRACT_OK, EMPTY_EXTRACT]})  # 13 has a claim, 11 none
    svc, client, _, _ = service(tmp_path, monkeypatch, pages=(), mubit=mubit)
    post(client, "/api/page/classify")
    drain(svc)
    assert mubit.sent == [E, C, E]
    [rule] = Store(svc.calls_path).rule_labels().values()
    assert rule["label"] == "maintenance" and rule["measured"]["size_delta"] == 5
    assert client.get("/api/edit/11").json()["classification"]["source"] == "rule"
    assert _state(client) == (0, 0, 0, None, False)
    assert post(client, "/api/page/classify").json()["already"] == 2 and len(mubit.sent) == 3


def test_a_missing_parent_makes_no_call_is_errored_and_never_queued_again(tmp_path, monkeypatch):
    wiki = wiki_session(tmp_path, fail=lambda p: _page_route(p, hidden=(12,)) if wikitext_of(13)(p) else None)
    svc, client, _, mubit = service(tmp_path, monkeypatch, wiki=wiki)
    post(client, "/api/page/classify")
    drain(svc)
    assert mubit.sent == [E, C]  # only revision 11
    assert Store(svc.calls_path).unavailable_diffs() == {13: "parent"}
    assert _state(client) == (0, 1, 0, None, True)
    again = post(client, "/api/page/classify").json()
    assert (again["queued"], again["errored"]) == (0, 1)


@pytest.mark.parametrize(
    "failure",
    [httpx.ConnectError("refused"), api_error()],  # a transport error; a plain SystemExit
    ids=["connection-error", "mediawiki-api-error"],
)
def test_a_failed_diff_fetch_drops_only_its_job_and_leaves_the_edit_open(tmp_path, monkeypatch, failure):
    wiki = wiki_session(tmp_path, fail=lambda p: failure if wikitext_of(13)(p) else None)
    svc, client, _, mubit = service(tmp_path, monkeypatch, wiki=wiki)
    post(client, "/api/page/classify")
    drain(svc)
    assert mubit.sent == [E, C]  # the worker went on to revision 11
    assert _state(client) == (0, 0, 1, None, True)
    assert Store(svc.calls_path).unavailable_diffs() == {}
    assert post(client, "/api/page/classify").json()["queued"] == 1


def test_wikipedia_budget_spent_mid_queue_drops_the_leftovers_with_wikipedia(tmp_path, monkeypatch):
    wiki = wiki_session(tmp_path, budget=6, reserve=5)  # one wikitext query, then the reserve
    svc, client, _, mubit = service(tmp_path, monkeypatch, pages=(JANE, JOHN), wiki=wiki)
    _classify_both(client)
    drain(svc)
    assert mubit.sent == [E, C]
    assert _state(client, JANE) == (0, 0, 1, "wikipedia", True)
    assert _state(client, JOHN) == (0, 0, 2, "wikipedia", True)
    r = post(client, "/api/page/classify", JOHN)
    assert (r.status_code, r.json()) == (429, {"reason": "wikipedia"})


def test_every_call_failing_leaves_the_window_errored_and_probes_change_nothing(tmp_path, monkeypatch):
    mubit = Mubit({E: ["x"] * 4})
    svc, client, _, _ = service(tmp_path, monkeypatch, mubit=mubit)
    post(client, "/api/page/classify")
    drain(svc)
    assert _state(client) == (0, 2, 0, None, True)
    store = Store(svc.calls_path)
    for revid in (11, 13):  # a probe run answers both steps
        ext = log_row(store, "probe", E, revid, extra=EMPTY_EXTRACT)
        log_row(store, "probe", C, revid, extra={"extract_call_id": ext})
    assert _state(client) == (0, 2, 0, None, True)
    again = post(client, "/api/page/classify").json()
    assert (again["already"], again["errored"], again["queued"]) == (0, 2, 0)


def test_an_answered_row_wins_over_a_final_error(tmp_path, monkeypatch):
    svc, client, _, _ = service(tmp_path, monkeypatch)
    post(client, "/api/page/classify")
    drain(svc)
    store = Store(svc.calls_path)
    first = log_row(store, "page", C, 13, error="unparseable reply: ValueError: x")
    log_row(store, "page", C, 13, error="unparseable reply: ValueError: x", extra={"retry_of": first})
    assert _state(client) == (0, 0, 0, None, False)
    assert post(client, "/api/page/classify").json()["already"] == 2
