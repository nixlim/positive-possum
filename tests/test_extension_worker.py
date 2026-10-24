"""The extension's service worker, run in node against a stubbed chrome API (tests/extension_harness.js)."""

import json
import shutil
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

import pytest

ROOT = Path(__file__).parent.parent
HARNESS = Path(__file__).parent / "extension_harness.js"
KINDS = ["page", "brief", "status", "brief-generate", "refresh", "classify"]
TITLE = "Keir Starmer"
PAGE = json.loads((ROOT / "extension" / "fixtures" / "page.json").read_text())
GET_PAGE = "GET /api/page"
REFRESH = "POST /api/page/refresh"
CLASSIFY = "POST /api/page/classify"
POSTS = {"POST /api/page/brief", REFRESH, CLASSIFY}
SESSION_KEY = "ingest:" + TITLE
AN_HOUR = 60 * 60 * 1000
GREY = {"color": "#75829a", "text": "–"}
HIGH = {"color": "#ff5470", "text": str(PAGE["analytics"]["flagged_30d"])}


def msg(kind: str, **extra) -> dict:
    return {"type": kind, "title": TITLE, **extra}


def worker(kind: str, backend, **settings) -> dict:
    return run([msg(kind)], backend, **settings)


def run(messages: list, backend, session: dict | None = None, **settings) -> dict:
    # Node is a gate requirement: these tests fail, never skip, without it.
    assert shutil.which("node"), "node is required to run the extension tests"
    scenario = {"settings": settings, "backend": backend, "messages": messages, "session": session or {}}
    r = subprocess.run(
        ["node", str(HARNESS), str(ROOT / "extension"), json.dumps(scenario)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize("kind", KINDS)
def test_an_unreachable_backend_shows_no_fixture_data_on_a_real_article(kind):
    out = worker(kind, "down")
    assert out["requests"], "the worker should have tried the backend"
    reply = out["reply"]
    assert reply["data"] is None
    assert reply["offline"] is False
    assert reply["error"] == "backend unreachable"
    assert "(fixture)" not in json.dumps(reply)


def test_an_unreachable_backend_leaves_the_badge_grey():
    out = worker("page", "down")
    assert out["badge"] == {"color": "#75829a", "text": "–"}


def test_demo_mode_shows_the_fixtures_without_calling_the_backend():
    out = worker("page", "down", demo=True)
    assert out["requests"] == []
    assert out["reply"]["offline"] is True
    assert out["reply"]["data"]["title"].endswith("(fixture)")


def test_a_title_off_the_watchlist_gets_no_data_and_a_grey_badge():
    out = worker("page", {"status": 404, "body": {"detail": "not on the watchlist"}})
    assert out["reply"]["data"] is None
    assert out["reply"]["status"] == 404
    assert out["badge"]["text"] == "–"


def ok(body: dict, status: int = 200) -> dict:
    return {"status": status, "body": body}


def calls(out: dict) -> list[str]:
    return [r["method"] + " " + urlsplit(r["url"]).path for r in out["requests"]]


def posts(out: dict) -> list[str]:
    return [c for c in calls(out) if c.startswith("POST ")]


def test_a_404_page_then_ingest_posts_one_refresh_and_a_second_ingest_sends_nothing():
    backend = {GET_PAGE: [ok({}, 404)], REFRESH: [ok(PAGE)]}
    out = run([msg("page"), msg("ingest"), msg("ingest")], backend)
    assert calls(out) == [GET_PAGE, REFRESH]
    first, ingested, again = out["replies"]
    assert first["status"] == 404
    assert ingested["data"]["title"] == PAGE["title"]
    assert again == ingested
    assert out["session"][SESSION_KEY]["data"] is None


@pytest.mark.parametrize(
    "refresh",
    [
        ok({}, 404),
        ok({"reason": "recent"}, 429),
        ok({"detail": {"reason": "wikipedia"}}, 429),
        ok({}, 502),
        "down",
    ],
    ids=["404", "429-recent", "429-wikipedia", "502", "unreachable"],
)
def test_ingest_is_sent_once_per_title_per_session_whatever_the_outcome(refresh):
    steps = [msg("page"), msg("ingest"), {"advance": AN_HOUR}, msg("page"), msg("ingest")]
    out = run(steps, {GET_PAGE: [ok({}, 404)], REFRESH: [refresh]})
    assert calls(out).count(REFRESH) == 1
    assert out["replies"][3] == out["replies"][1]  # `advance` adds no reply


def test_ingest_is_sent_once_across_tabs():
    steps = [msg("page", tab=1), msg("page", tab=2), msg("ingest", tab=1), msg("ingest", tab=2)]
    out = run(steps, {GET_PAGE: [ok({}, 404)], REFRESH: [ok({}, 502)]})
    assert posts(out) == [REFRESH]
    assert out["replies"][3] == out["replies"][2]
    assert out["replies"][3]["status"] == 502


def test_two_tabs_asking_at_once_send_one_ingest():
    steps = [
        msg("page", tab=1),
        msg("page", tab=2),
        {"together": [msg("ingest", tab=1), msg("ingest", tab=2)]},
    ]
    out = run(steps, {GET_PAGE: [ok({}, 404)], REFRESH: [ok(PAGE)]})
    assert posts(out) == [REFRESH]
    one, two = out["replies"][2:]
    assert one == two
    assert one["data"]["title"] == PAGE["title"]
    assert out["badges"]["1"] == out["badges"]["2"] == HIGH


def test_a_restarted_worker_still_finds_the_title_in_session_storage():
    steps = [msg("page"), msg("ingest"), {"restart": True}, msg("page"), msg("ingest")]
    out = run(steps, {GET_PAGE: [ok({}, 404)], REFRESH: [ok({"reason": "recent"}, 429)]})
    assert calls(out).count(REFRESH) == 1
    again = out["reply"]
    assert again["status"] == 429
    assert again["reason"] == "recent"


def test_a_title_already_in_session_storage_gets_no_ingest():
    seeded = {
        SESSION_KEY: {"data": None, "offline": False, "status": 404, "error": "HTTP 404", "reason": None}
    }
    out = run([msg("page"), msg("ingest")], {GET_PAGE: [ok({}, 404)]}, session=seeded)
    assert calls(out) == [GET_PAGE]
    assert out["reply"] == seeded[SESSION_KEY]


def test_the_session_record_is_written_before_the_refresh_is_sent():
    out = run([msg("page"), msg("ingest")], {GET_PAGE: [ok({}, 404)], REFRESH: [ok(PAGE)]})
    get, refresh = out["requests"]
    assert SESSION_KEY not in get["session"]
    assert SESSION_KEY in refresh["session"]


def test_try_again_sends_one_refresh_per_click_even_after_the_ingest():
    steps = [msg("page"), msg("ingest"), msg("refresh"), msg("refresh")]
    out = run(steps, {GET_PAGE: [ok({}, 404)], REFRESH: [ok({}, 502), ok({}, 502), ok(PAGE)]})
    assert posts(out) == [REFRESH, REFRESH, REFRESH]
    assert out["reply"]["data"]["title"] == PAGE["title"]
    assert out["badge"] == HIGH


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (ok({"reason": "recent"}, 429), "recent"),
        (ok({"detail": {"reason": "wikipedia"}}, 429), "wikipedia"),
        (ok({"detail": "Not Found"}, 404), None),
        ({"status": 502}, None),
    ],
    ids=["top-level", "fastapi-detail", "detail-string", "no-json-body"],
)
def test_a_refused_request_passes_the_backend_reason_through(answer, reason):
    out = worker("refresh", answer)
    assert out["reply"] == {
        "data": None,
        "offline": False,
        "status": answer["status"],
        "error": "HTTP " + str(answer["status"]),
        "reason": reason,
    }


@pytest.mark.parametrize(
    ("messages", "pages"),
    [
        ([msg("ingest")], [ok(PAGE)]),
        ([msg("page"), msg("ingest")], [ok(PAGE)]),
        ([msg("page"), msg("page"), msg("ingest")], [ok({}, 404), ok(PAGE)]),
    ],
    ids=["no-page-reply", "after-200", "404-then-200"],
)
def test_ingest_without_a_404_as_the_last_page_reply_sends_nothing(messages, pages):
    out = run(messages, {GET_PAGE: pages})
    assert REFRESH not in calls(out)
    assert out["reply"]["error"] == "not ingesting"


def test_demo_mode_and_an_unreachable_backend_never_post_on_ingest():
    demo = run([msg("page"), msg("ingest")], "down", demo=True)
    assert demo["requests"] == []
    down = run([msg("page"), msg("ingest")], "down")
    assert calls(down) == [GET_PAGE]
    assert down["reply"]["error"] == "not ingesting"


def test_a_successful_ingest_colours_the_badge_and_a_failed_one_leaves_it_grey():
    good = run([msg("page"), msg("ingest")], {GET_PAGE: [ok({}, 404)], REFRESH: [ok(PAGE)]})
    assert good["badge"] == HIGH
    bad = run([msg("page"), msg("ingest")], {GET_PAGE: [ok({}, 404)], REFRESH: [ok({}, 404)]})
    assert bad["badge"] == GREY


def test_classify_posts_once_and_drops_the_cached_page():
    queued = {"queued": 3, "already": 1, "errored": 0, "pending": 0, "skipped": 0, "remaining": 200}
    backend = {GET_PAGE: [ok(PAGE)], CLASSIFY: [ok(queued, 202)]}
    out = run([msg("page"), msg("page"), msg("classify"), msg("page")], backend)
    assert calls(out) == [GET_PAGE, CLASSIFY, GET_PAGE]
    assert out["replies"][2] == {"data": queued, "offline": False, "status": 202}


def test_a_fresh_page_read_refetches_and_refills_the_cache():
    out = run([msg("page"), msg("page", fresh=True), msg("page")], {GET_PAGE: [ok(PAGE)]})
    assert calls(out) == [GET_PAGE, GET_PAGE]


@pytest.mark.parametrize("score", [5.0, 50.0, 95.0])
def test_a_partial_window_leaves_the_badge_grey_whatever_the_score(score):
    page = {**PAGE, "risk_partial": True, "risk_score": score}
    assert worker("page", ok(page))["badge"] == GREY


def test_a_complete_window_shows_the_flag_count_without_a_plus():
    assert worker("page", ok(PAGE))["badge"] == HIGH


def test_the_worker_sends_no_post_beyond_brief_refresh_and_classify():
    backend = {
        GET_PAGE: [ok({}, 404), ok(PAGE)],
        "GET /api/page/brief": [ok({}, 404)],
        "GET /api/mubit/status": [ok({})],
        "POST /api/page/brief": [ok({})],
        REFRESH: [ok(PAGE)],
        CLASSIFY: [ok({}, 202)],
    }
    # A 404 then a 200 page read: the ingest after them is refused, so its POST is not sent.
    out = run([msg("page"), *[msg(k) for k in [*KINDS, "ingest"]]], backend)
    assert set(posts(out)) == POSTS
    assert len(posts(out)) == 3
