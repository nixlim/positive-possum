"""The live feed and the Wikipedia reserve (PIPELINE.md section 9)."""

import threading

from api_fakes import (
    JANE,
    WAIT_S,
    Mubit,
    Stream,
    api_error,
    event,
    joined,
    page,
    post,
    service,
    wiki_session,
)
from test_pipeline import _rows

from triage import steps
from triage import watch_views as views
from triage.caller import Caller
from triage.store import Store

E, C = steps.EXTRACT, steps.CLASSIFY
LIVE_KEYS = {"revid", "title", "user_kind", "label", "severity", "review", "ts", "served", "stopped"}


def _handle(svc, ev):
    return svc.live.handle(ev, Caller(svc.client, svc.store(), "live", limiter=svc.limiter))


def test_the_reader_queues_only_watched_main_namespace_human_edits(tmp_path, monkeypatch):
    svc, _, _, _ = service(tmp_path, monkeypatch)
    events = [event(), event(title="Unwatched"), event(namespace=1), event(bot=True), event(revid=22)]
    svc.live.read_stream(Stream(events), threading.Event())
    assert [svc.live.queue.get_nowait()["revision"]["new"] for _ in range(2)] == [21, 22]
    assert svc.live.queue.empty()


def test_a_live_edit_is_classified_once_and_kept_for_new_clients(tmp_path, monkeypatch):
    svc, client, wiki, mubit = service(tmp_path, monkeypatch)
    ev = _handle(svc, event())
    assert set(ev) == LIVE_KEYS
    got = (ev["revid"], ev["label"], ev["severity"], ev["review"], ev["stopped"])
    assert got == (21, "content", 1, True, None)
    assert ev["served"] == {"extract": "adapter-v1", "classify": "adapter-v1"}
    assert mubit.sent == [E, C] and svc.live.snapshot() == {"edits": [ev]}
    assert client.get("/api/edit/21").json()["classification"]["label"] == "content"
    assert {r["kind"] for r in _rows(Store(svc.calls_path))} == {"live"} and wiki.sent.total() > 0


def test_a_live_edit_after_the_mubit_budget_is_spent_is_stopped_budget(tmp_path, monkeypatch):
    svc, _, wiki, mubit = service(tmp_path, monkeypatch, budget=1, live_reserve=1)
    ev = _handle(svc, event())
    assert (ev["label"], ev["stopped"]) == (None, "budget") and mubit.sent == [E]
    sent = wiki.sent.total()
    ev = _handle(svc, event(revid=22))
    assert (ev["label"], ev["severity"], ev["review"], ev["stopped"]) == (None, None, None, "budget")
    assert mubit.sent == [E] and wiki.sent.total() == sent  # no call and no fetch once spent


def test_refresh_and_classify_stop_at_the_wikipedia_reserve_while_live_still_fetches(tmp_path, monkeypatch):
    wiki = wiki_session(tmp_path, budget=6, reserve=6)
    svc, client, _, mubit = service(tmp_path, monkeypatch, wiki=wiki)
    for path in ("/api/page/refresh", "/api/page/classify"):
        r = post(client, path, "Jane Doe Two" if path.endswith("refresh") else JANE)
        assert (r.status_code, r.json()) == (429, {"reason": "wikipedia"}), path
    assert wiki.sent.total() == 0
    ev = _handle(svc, event())
    # fetch_revisions: the revision, its parent's size, the account; then one wikitext query
    assert ev["label"] == "content" and ev["stopped"] is None and wiki.sent.total() == 4
    assert client.get("/api/mubit/status").json()["wikipedia"] == {"spent": 4, "remaining": 2, "reserve": 6}


def test_a_refresh_on_another_thread_is_refused_while_live_is_inside_the_reserve(tmp_path, monkeypatch):
    wiki = wiki_session(tmp_path, budget=6, reserve=6)
    _, client, _, _ = service(tmp_path, monkeypatch, pages=(), wiki=wiki)
    inside, done = threading.Event(), threading.Event()

    def live():
        with wiki.using_reserve():
            inside.set()
            done.wait(WAIT_S)

    t = threading.Thread(target=live)
    t.start()
    assert inside.wait(WAIT_S)
    r = post(client, "/api/page/refresh", JANE)
    done.set()
    t.join(WAIT_S)
    assert not t.is_alive()
    assert (r.status_code, r.json()) == (429, {"reason": "wikipedia"}) and wiki.log == []


def test_a_live_edit_after_the_wikipedia_budget_is_spent_is_stopped_wikipedia(tmp_path, monkeypatch):
    svc, _, wiki, mubit = service(tmp_path, monkeypatch, wiki=wiki_session(tmp_path, budget=0))
    ev = _handle(svc, event())
    assert (ev["label"], ev["severity"], ev["review"], ev["stopped"]) == (None, None, None, "wikipedia")
    assert mubit.sent == [] and wiki.log == []


def test_an_api_error_on_a_live_edit_leaves_both_threads_running(tmp_path, monkeypatch):
    def fail(p):
        return api_error() if p.get("revids") == "21" and p.get("rvprop") != "ids|content" else None

    wiki = wiki_session(tmp_path, fail=fail)
    svc, client, _, mubit = service(tmp_path, monkeypatch, wiki=wiki, live=True)
    svc.start(None)
    svc.live.queue.put(event())
    svc.live.queue.put(event(revid=22))
    assert joined(svc.live.queue), "the live worker stopped"
    assert post(client, "/api/page/classify").status_code == 202
    assert joined(svc.queue.jobs), "the classify worker stopped"
    svc.stop()
    first, second = list(svc.live.recent)[::-1]
    assert (first["revid"], first["label"], first["stopped"]) == (21, None, None)  # transient: neither budget
    assert (second["revid"], second["label"]) == (22, "content")
    assert page(client)["classify_pending"] == 0 and page(client)["classify_open"] == 0
    assert mubit.sent == [E, C] * 3


def test_a_live_row_without_a_result_has_the_null_fields():
    rc = {"revid": 9, "title": JANE, "timestamp": "2026-10-24T12:00:00Z", "anon": True, "temp": False}
    ev = views.live_event(rc, None, "wikipedia")
    assert set(ev) == LIVE_KEYS and ev["stopped"] == "wikipedia"
    assert (ev["label"], ev["severity"], ev["review"]) == (None, None, None)
    assert Mubit().sent == []
