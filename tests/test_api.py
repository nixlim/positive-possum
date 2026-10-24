import json
from pathlib import Path

import pytest
from api_fakes import JANE, api_error, drain, page, post, service, wiki_session
from test_pages import AS_OF, FakeSession, _page_route

from triage import api, steps, titles
from triage import pages as pg
from triage.watch import Options, Service

FIXTURES = Path(__file__).parent.parent / "extension" / "fixtures"
CLASSIFY_KEYS = {"classify_pending", "classify_errored", "classify_open", "classify_stopped"}


def _fixture(name):
    return json.loads((FIXTURES / name).read_text())


def test_endpoint_keys_match_the_extension_fixtures(tmp_path, monkeypatch):
    svc, client, _, _ = service(tmp_path, monkeypatch)
    assert post(client, "/api/page/classify").status_code == 202
    drain(svc)
    got, fixture = page(client), _fixture("page.json")
    assert set(got) == set(fixture) | CLASSIFY_KEYS
    assert {k: got[k] for k in CLASSIFY_KEYS} == {
        "classify_pending": 0,
        "classify_errored": 0,
        "classify_open": 0,
        "classify_stopped": None,
    }
    assert got["risk_partial"] is False
    assert [f["revid"] for f in got["flags"]] == [13, 11]  # newest first
    flag, want = got["flags"][0], fixture["flags"][0]
    assert set(flag) == set(want)
    assert set(flag["classification"]) == set(want["classification"])
    assert set(want["measured"]) <= set(flag["measured"])
    assert flag["diff_url"] == "https://en.wikipedia.org/w/index.php?diff=13&oldid=12"

    brief = post(client, "/api/page/brief")
    assert set(brief.json()) == set(_fixture("brief.json"))
    assert client.get("/api/page/brief", params={"title": JANE}).json() == brief.json()

    status, want = client.get("/api/mubit/status").json(), _fixture("status.json")
    assert set(status) == set(want) | {"mubit", "wikipedia"}
    for step in want:
        assert set(status[step]) == set(want[step])
    assert status[steps.CLASSIFY]["requests"] == 2 and status[steps.BRIEF]["requests"] == 1
    assert status["mubit"] == {"spent": 5, "remaining": 295}
    assert status["wikipedia"] == {"spent": 2, "remaining": 98, "reserve": 0}  # two wikitext queries

    [row] = client.get("/api/watchlist").json()
    assert set(row) == set(_fixture("watchlist.json")[0])
    assert row["flagged_30d"] == 2 and len(row["spark"]) == 30


def test_status_copies_the_console_fields_per_step(tmp_path, monkeypatch):
    svc, client, _, _ = service(tmp_path, monkeypatch)
    (tmp_path / "mubit_status.json").write_text(json.dumps({steps.EXTRACT: {"kept_examples": 12}}))
    status = client.get("/api/mubit/status").json()
    assert status[steps.EXTRACT]["console"] == {"kept_examples": 12}
    assert status[steps.CLASSIFY]["console"] == {} and status[steps.CLASSIFY]["requests"] == 0
    (tmp_path / "mubit_status.json").unlink()
    assert svc.status()[steps.EXTRACT]["console"] == {}


def test_cost_is_priced_from_the_logged_tokens(tmp_path, monkeypatch):
    svc, client, _, _ = service(tmp_path, monkeypatch)
    assert client.get("/api/cost").json()["provider_per_1k_edits"] is None
    post(client, "/api/page/classify")
    drain(svc)
    cost = client.get("/api/cost").json()
    assert cost["calls_per_edit"] == 2 and cost["tokens_per_call"] == {"input": 10, "output": 3}
    assert cost["provider_per_1k_edits"] == round(1000 * 2 * (10 * 2.0 + 3 * 10.0) / 1e6, 2)
    assert cost["price"] == {"input": 2.0, "output": 10.0} and cost["assumptions"]


def test_a_post_needs_an_allowed_origin_and_cors_answers_only_those(tmp_path, monkeypatch):
    ext = "chrome-extension://abcdef"
    _, client, _, mubit = service(tmp_path, monkeypatch, origins=[*api.ORIGINS, ext])
    url, title = "/api/page/classify", {"title": JANE}
    for path in (url, "/api/page/refresh", "/api/page/brief"):
        assert client.post(path, params=title).status_code == 403
    assert client.post(url, params=title, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post(url, params=title, headers={"Origin": ext}).status_code == 202
    assert client.post("/api/page/brief", params=title, headers={"Origin": "null"}).status_code == 403
    assert mubit.sent == []  # no worker running, and the refused brief never reached Mubit

    ok = client.get("/api/watchlist", headers={"Origin": ext})
    assert ok.headers["access-control-allow-origin"] == ext
    other = client.get("/api/watchlist", headers={"Origin": "http://evil.example"})
    assert "access-control-allow-origin" not in other.headers
    pre = {"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"}
    assert "access-control-allow-origin" not in client.options(url, headers=pre).headers
    pre["Origin"] = ext
    assert client.options(url, headers=pre).headers["access-control-allow-origin"] == ext
    assert client.get("/api/watchlist", headers={"host": "evil.example"}).status_code == 400


def test_no_get_touches_mubit_or_wikipedia(tmp_path, monkeypatch):
    svc, client, wiki, mubit = service(tmp_path, monkeypatch)
    post(client, "/api/page/classify")
    drain(svc)
    sent, requests = list(mubit.sent), wiki.sent.total()
    for path in ("/", "/api/watchlist", "/api/mubit/status", "/api/cost", "/api/edit/11"):
        assert client.get(path).status_code == 200, path
    assert client.get("/api/page", params={"title": JANE}).status_code == 200
    assert client.get("/api/page", params={"title": "Never Ingested"}).status_code == 404
    assert client.get("/api/page/brief", params={"title": JANE}).status_code == 404
    assert client.get("/api/edit/999").status_code == 404
    assert (mubit.sent, wiki.sent.total()) == (sent, requests)


def test_watch_html_builds_its_dom_without_inner_html_and_names_the_budgets(tmp_path, monkeypatch):
    _, client, _, _ = service(tmp_path, monkeypatch)
    html = client.get("/").text
    assert "innerHTML" not in html and "/api/live" in html
    assert "not classified: Mubit budget" in html and "not classified: Wikipedia budget" in html


def test_refresh_ingests_within_the_request_budget_and_calls_no_model(tmp_path, monkeypatch):
    wiki = wiki_session(tmp_path, budget=12)
    _, client, _, mubit = service(tmp_path, monkeypatch, pages=(), wiki=wiki)
    assert client.get("/api/page", params={"title": "JD"}).status_code == 404 and wiki.log == []
    r = post(client, "/api/page/refresh", "JD")
    assert r.status_code == 200 and r.json()["title"] == JANE and r.json()["flags"] == []
    assert r.json()["classify_open"] == 2 and r.json()["risk_partial"] is True
    assert wiki.sent.total() <= 12 and mubit.sent == []
    assert page(client, JANE)["pageid"] == page(client, "JD")["pageid"] == 5  # the redirect found it
    assert client.get("/api/watchlist").json()[0]["title"] == JANE


def test_refresh_is_limited_per_resolved_pageid(tmp_path, monkeypatch):
    _, client, wiki, _ = service(tmp_path, monkeypatch, pages=())
    assert post(client, "/api/page/refresh", JANE).status_code == 200
    sent = wiki.sent.total()
    again = post(client, "/api/page/refresh", "JD")  # another title of the same page
    assert (again.status_code, again.json()) == (429, {"reason": "recent"})
    assert wiki.sent.total() == sent + 1  # the one query that resolved the new title


def test_a_missing_page_or_another_namespace_is_404_and_not_recorded(tmp_path, monkeypatch):
    svc, client, _, _ = service(tmp_path, monkeypatch, pages=())
    for title in ("Talk:Jane Doe", "Nobody"):
        assert post(client, "/api/page/refresh", title).status_code == 404
        assert post(client, "/api/page/refresh", title).status_code == 404  # no 10-minute claim
    assert titles.load(svc.pages_dir) == {}
    assert post(client, "/api/page/classify", "Nobody").status_code == 404


def test_the_title_index_survives_a_restart(tmp_path, monkeypatch):
    _, client, _, _ = service(tmp_path, monkeypatch, pages=())
    assert post(client, "/api/page/refresh", "JD").status_code == 200
    _, restarted, _, _ = service(tmp_path, monkeypatch, pages=())
    assert page(restarted, "JD")["pageid"] == 5


def test_a_snapshot_written_by_pages_ingest_is_found_by_its_title(tmp_path, monkeypatch):
    pg.ingest(FakeSession(_page_route), ["JD"], AS_OF, 90, tmp_path / "pages" / "snapshots")
    assert titles.load(tmp_path / "pages") == {"JD": 5, JANE: 5}
    _, client, _, _ = service(tmp_path, monkeypatch, pages=())
    assert page(client, "JD")["title"] == page(client, JANE)["title"] == JANE


def test_snapshots_written_before_the_index_are_indexed_at_startup(tmp_path, monkeypatch):
    svc, client, _, _ = service(tmp_path, monkeypatch)  # writes the snapshot file only
    assert titles.load(svc.pages_dir) == {JANE: 5} and page(client)["pageid"] == 5


@pytest.mark.parametrize("asked", ["Jane_Doe", "jane Doe", "Jane%20Doe", "  Jane   Doe "])
def test_spellings_of_a_title_find_the_same_snapshot(tmp_path, monkeypatch, asked):
    _, client, _, _ = service(tmp_path, monkeypatch)
    assert page(client, asked)["pageid"] == 5


def test_a_refresh_refused_by_the_reserve_mid_ingest_writes_nothing(tmp_path, monkeypatch):
    wiki = wiki_session(tmp_path, budget=8, reserve=5)  # the lookup and two ingest requests
    svc, client, _, _ = service(tmp_path, monkeypatch, pages=(), wiki=wiki)
    r = post(client, "/api/page/refresh", JANE)
    assert (r.status_code, r.json()) == (429, {"reason": "wikipedia"})
    assert wiki.sent.total() == 3 and len(wiki.log) == 3
    assert not list(svc.snap_dir.glob("*")) and titles.load(svc.pages_dir) == {}
    assert client.get("/api/watchlist").status_code == 200  # the API goes on


def test_an_api_error_during_refresh_is_502_and_writes_nothing(tmp_path, monkeypatch):
    wiki = wiki_session(tmp_path, fail=lambda p: api_error() if p.get("action") == "parse" else None)
    svc, client, _, _ = service(tmp_path, monkeypatch, pages=(), wiki=wiki)
    assert post(client, "/api/page/refresh", JANE).status_code == 502
    assert not list(svc.snap_dir.glob("*")) and titles.load(svc.pages_dir) == {}
    assert post(client, "/api/page/refresh", JANE).status_code == 502  # the 10 minutes were given back


def test_the_cli_reserves_reach_the_service_options(monkeypatch):
    from triage import cli, watch

    seen = {}
    monkeypatch.setattr(api, "serve", lambda svc, *_a: seen.update(opts=svc))
    monkeypatch.setattr(watch, "Service", lambda opts, *_a: opts)
    cli.main(["api", "--titles", JANE, "--live-reserve", "7", "--wiki-live-reserve", "9", "--no-live"])
    assert (seen["opts"].live_reserve, seen["opts"].wiki_live_reserve) == (7, 9)
    cli.main(["api", "--titles", JANE])
    assert (seen["opts"].live_reserve, seen["opts"].wiki_live_reserve) == (60, 300)
    assert (Options().live_reserve, Options().wiki_live_reserve) == (60, 300)


def test_the_service_session_keeps_the_wikipedia_reserve(tmp_path, monkeypatch):
    monkeypatch.setattr(pg, "default_as_of", lambda: AS_OF)
    opts = Options(watchlist=None, titles=[JANE], request_budget=50, wiki_live_reserve=20, live=False)
    s = Service(opts, tmp_path, lambda: None).session
    assert (s.budget, s.reserve) == (50, 20)
