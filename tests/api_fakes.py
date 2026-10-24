"""Fakes for the watchlist API tests: a real corpus.Session over a mock transport (so its budget,
reserve and cache are the real ones), a scripted Mubit client and the EventStreams stream."""

import json
import threading
from types import SimpleNamespace

import anthropic
import httpx
from fastapi.testclient import TestClient
from test_pages import AS_OF, FakeSession, _page_route

from triage import api, pipeline, steps
from triage import corpus as cp
from triage import pages as pg
from triage.llm import CallResult
from triage.store import Store
from triage.watch import Options, Service

ORIGIN = "http://127.0.0.1:8322"
POST = {"headers": {"Origin": ORIGIN}}
JANE, JOHN = "Jane Doe", "John Roe"  # pageid 5 (revisions 11, 13) and 6 (111, 113)
EXTRACT_OK = {
    "claims_added": ["Doe was fined"],
    "claims_removed": [],
    "negative_info": "added",
    "tone_shift": -1,
}
EMPTY_EXTRACT = {"claims_added": [], "claims_removed": [], "negative_info": "none", "tone_shift": 0}
CLASSIFY_OK = {
    "label": "content",
    "severity": 1,
    "review": True,
    "confidence": "high",
    "rationale": "A fine.",
}
WAIT_S = 10  # every wait on a thread is bounded, so a stuck worker fails its test
BRIEF_MD = "## Jane Doe\nOne fine added."


def api_error(_p=None):
    """The reply wiki._api turns into a plain SystemExit."""
    return {"error": {"code": "internal_api_error", "info": "boom"}}


def rate_limited():
    response = httpx.Response(429, request=httpx.Request("POST", "https://mubit.example/v1/messages"))
    return anthropic.RateLimitError("rate limited", response=response, body=None)


def route(p):
    """The pages test route, plus the title lookup refresh makes and the live revision fetch by
    revid (any revid, parent 13, 60 bytes added). The window and before-window queries also ask
    for REV_PROPS, by title or pageid, and go to the pages route."""
    if p.get("prop") == "info":
        found = {
            JANE: {"pageid": 5, "ns": 0, "title": JANE},
            "JD": {"pageid": 5, "ns": 0, "title": JANE},  # a redirect
            "Talk:Jane Doe": {"pageid": 6, "ns": 1, "title": "Talk:Jane Doe"},
        }.get(p["titles"], {"title": p["titles"], "missing": True})
        return {"query": {"pages": [found]}}
    ids = [int(r) for r in p.get("revids", "").split("|") if r]
    live = {"revid": 0, "parentid": 13, "timestamp": "2026-10-24T12:00:00Z", "user": "Alice", "userid": 7}
    fetched = {
        pipeline.REV_PROPS: [live | {"revid": i, "comment": "tweak", "size": 1050, "tags": []} for i in ids],
        "ids|size": [{"revid": i, "size": 990} for i in ids],
    }
    if ids and p.get("rvprop") in fetched:
        page_ = {"pageid": 5, "ns": 0, "title": JANE, "revisions": fetched[p["rvprop"]]}
        return {"query": {"pages": [page_]}}
    return _page_route(p)


def wiki_session(tmp_path, budget=100, reserve=0, fail=lambda _p: None):
    """A real Session whose requests `route` answers; `fail(params)` may return a reply or an
    exception to raise instead. `.log` holds every request's params."""
    s = cp.Session(tmp_path / "wikicache", budget, per_min=1e9, reserve=reserve)
    s.log = []

    def handler(request):
        params = dict(request.url.params)
        s.log.append(params)
        got = fail(params)
        if isinstance(got, BaseException):
            raise got
        return httpx.Response(200, json=route(params) if got is None else got)

    s.client = httpx.Client(transport=httpx.MockTransport(handler), event_hooks={"request": [s._on_request]})
    return s


def wikitext_of(revid):
    """A `fail` matcher: the wikitext query that fetches `revid`."""
    return lambda p: p.get("rvprop") == "ids|content" and str(revid) in p["revids"].split("|")


class Mubit:
    """Answers each step from its script (a reply dict or text, or an exception to raise), then
    with a fixed good reply; `.sent` records the step of every request."""

    def __init__(self, script=None, classify=None):
        self.script = {step: list(replies) for step, replies in (script or {}).items()}
        self.default = {
            steps.EXTRACT: EXTRACT_OK,
            steps.CLASSIFY: classify or CLASSIFY_OK,
            steps.BRIEF: BRIEF_MD,
        }
        self.sent, self.lock = [], threading.Lock()
        self.messages = SimpleNamespace(with_raw_response=SimpleNamespace(create=self.create))

    def create(self, **req):
        step = next(s for s, p in steps.SYSTEMS.items() if p == req["system"])
        with self.lock:
            self.sent.append(step)
            queued = self.script.get(step)
            reply = queued.pop(0) if queued else self.default[step]
            n = len(self.sent)
        if isinstance(reply, BaseException):
            raise reply
        text = reply if isinstance(reply, str) else json.dumps(reply)
        msg = anthropic.types.Message.model_validate(
            {
                "id": "msg_1",
                "type": "message",
                "role": "assistant",
                "model": "adapter-v1",
                "content": [{"type": "text", "text": text}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 10, "output_tokens": 3},
            }
        )
        headers = httpx.Headers({"x-middleware-request-id": f"mreq_{n}"})
        return SimpleNamespace(headers=headers, parse=lambda: msg)


class Stream:
    """httpx.Client.stream for live.iter_events: the given events as SSE data lines."""

    def __init__(self, events=()):
        self.lines = [": ok", *(f"data: {json.dumps(e)}" for e in events)]

    def stream(self, *_a, **_kw):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def raise_for_status(self):
        pass

    def iter_lines(self):
        return iter(self.lines)


def snapshot(title=JANE):
    """The pages test snapshot of Jane Doe, or the same page moved to John Roe (pageid 6,
    revids + 100)."""
    snap = pg.ingest_page(FakeSession(_page_route), JANE, AS_OF, 90)
    if title == JOHN:
        snap["title"], snap["pageid"] = JOHN, 6
        snap["revisions"] = [
            r | {"revid": r["revid"] + 100, "parentid": r["parentid"] + 100} for r in snap["revisions"]
        ]
    return snap


def service(  # noqa: PLR0913 - test knobs
    tmp_path,
    monkeypatch,
    pages=(JANE,),
    budget=300,
    live_reserve=0,
    wiki=None,
    mubit=None,
    origins=api.ORIGINS,
    live=False,
):
    """A Service over tmp_path with the given pages' snapshots written before it starts."""
    monkeypatch.setattr(pg, "default_as_of", lambda: AS_OF)
    for title in pages:
        pg.write_snapshot(snapshot(title), tmp_path / "pages" / "snapshots")
    wiki = wiki if wiki is not None else wiki_session(tmp_path)
    mubit = mubit or Mubit()
    opts = Options(
        watchlist=None,
        titles=[JANE, JOHN],
        call_budget=budget,
        min_interval=0,
        live_reserve=live_reserve,
        live=live,
    )
    svc = Service(opts, tmp_path, lambda: mubit, session=wiki, http_factory=Stream)
    client = TestClient(api.create_app(svc, origins=origins), base_url=ORIGIN)
    return svc, client, wiki, mubit


def joined(q) -> bool:
    """q.join() within WAIT_S: False when the queue still has unfinished work."""
    t = threading.Thread(target=q.join, daemon=True)
    t.start()
    t.join(WAIT_S)
    return not t.is_alive()


def drain(svc):
    """Starts the service's threads and waits until the classify queue is empty."""
    svc.start(None)
    assert joined(svc.queue.jobs), "the classify worker never emptied its queue"
    svc.stop()


def post(client, path, title=JANE):
    return client.post(path, params={"title": title}, **POST)


def page(client, title=JANE):
    return client.get("/api/page", params={"title": title}).json()


def event(revid=21, title=JANE, **kw):
    return {
        "type": "edit",
        "server_name": "en.wikipedia.org",
        "namespace": 0,
        "bot": False,
        "title": title,
        "revision": {"old": 13, "new": revid},
        "comment": "tweak",
        "timestamp": 1792843200,
        "user": "Alice",
        "length": {"old": 990, "new": 1050},
    } | kw


def log_row(store: Store, kind, step, revid, error=None, extra=None):
    """A logged call, as pipeline.Caller would log it."""
    res = CallResult([], "m", "adapter-v1", "r", 1, 1, 1, error=error, extra=extra or {})
    return store.log_call(f"{kind}-test", kind, step, revid, res)
