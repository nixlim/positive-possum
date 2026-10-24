"""The watchlist API's state (PIPELINE.md section 9): the watchlist, the title index over the
snapshots on disk, one Mubit limiter and one Wikipedia session shared by every call the process
makes, the classify queue (classify_queue.py) and the live feed (watch_live.py).

A sqlite connection may only be used on the thread that opened it, so every request and every
worker thread opens its own Store on the same file. Only POSTs and the worker threads reach
Mubit or Wikipedia; the read methods use the disk and the call log."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from anthropic import Anthropic

from . import corpus as cp
from . import edit_state as es
from . import pages as pg
from . import pipeline, provenance, titles
from . import watch_views as views
from .caller import Caller, Limiter
from .classify_queue import BUDGET, WIKIPEDIA, ClassifyQueue
from .config import MODEL
from .store import Store
from .watch_live import LiveFeed
from .wiki import USER_AGENT

REFRESH_EVERY_S = 600


@dataclass
class Options:
    watchlist: str | None = "mps"
    titles: list[str] | None = None
    call_budget: int = 300
    min_interval: float = 2.0
    live_reserve: int = 60  # Mubit calls only the live feed may spend
    request_budget: int = 2000
    wiki_live_reserve: int = 300  # Wikipedia requests only the live feed may spend
    live: bool = True
    model: str = MODEL
    days: int = 90


class Refused(Exception):
    """A request the service will not serve: the HTTP status and a short reason."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def _fetch(fn: Callable):
    """fn's Wikipedia requests: refused by the budget (or at the live reserve) is 429
    "wikipedia"; any other failure, a MediaWiki API error's plain SystemExit included, is 502."""
    try:
        return fn()
    except cp.BudgetSpent:
        raise Refused(429, WIKIPEDIA) from None
    except (Exception, SystemExit) as e:
        raise Refused(502, f"{type(e).__name__}: {e}") from None


class Service:
    def __init__(
        self,
        opts: Options,
        data_dir: Path,
        client_factory: Callable[[], Anthropic],
        session: cp.Session | None = None,
        http_factory: Callable[[], httpx.Client] | None = None,
    ):
        self.opts, self.data_dir = opts, data_dir
        self.calls_path = data_dir / "calls.sqlite"
        self.pages_dir = data_dir / "pages"
        self.snap_dir = self.pages_dir / "snapshots"
        self.brief_dir = self.pages_dir / "briefs"
        self.session = session or cp.Session(
            self.pages_dir, opts.request_budget, reserve=opts.wiki_live_reserve
        )
        self.client = client_factory()
        self.limiter = Limiter(opts.call_budget, opts.min_interval, opts.live_reserve)
        self.lock, self.brief_lock = threading.Lock(), threading.Lock()
        self.stopping = threading.Event()
        self.workers: list[threading.Thread] = []
        snaps = list(pipeline.snapshots(self.snap_dir))
        if snaps:  # snapshots written before the index existed are found by title too
            titles.record(self.pages_dir, {s["title"]: s["pageid"] for s in snaps})
        self.revs = {r["revid"]: s["pageid"] for s in snaps for r in s["revisions"]}
        self.watchlist = opts.titles or pg.watchlist(
            self.session, pg.WATCHLISTS[opts.watchlist], pg.default_as_of()
        )
        self.refreshed: dict[int, float] = {}  # pageid -> when its last refresh started
        self.queue = ClassifyQueue(self.limiter, self._run_one)
        http_factory = http_factory or (lambda: httpx.Client(headers={"User-Agent": USER_AGENT}))
        self.live = LiveFeed(self.session, self.limiter, self._run, set(self.watchlist), http_factory)

    # ---- lifecycle ------------------------------------------------------------

    def _worker(self, kind: str, work: Callable) -> None:
        work(Caller(self.client, self.store(), kind, limiter=self.limiter))

    def start(self, loop: asyncio.AbstractEventLoop | None) -> None:
        self.stopping.clear()
        self.live.loop = loop
        workers = [("page", self.queue.work)] + ([("live", self.live.work)] if self.opts.live else [])
        self.workers = [
            threading.Thread(target=self._worker, args=(kind, work), name=f"api-{kind}", daemon=True)
            for kind, work in workers
        ]
        for t in self.workers:
            t.start()
        if self.opts.live:
            threading.Thread(target=self.live.reader, args=(self.stopping,), daemon=True).start()

    def stop(self) -> None:
        """Ends the workers once their queued work is done (None is their stop request)."""
        self.stopping.set()
        self.queue.jobs.put(None)
        if self.opts.live:
            self.live.queue.put(None)
        for t in self.workers:
            t.join()

    # ---- reads: disk and the call log only ------------------------------------

    def store(self) -> Store:
        return Store(self.calls_path)

    def _snapshot(self, title: str) -> dict | None:
        pageid = titles.lookup(self.pages_dir, title)
        if pageid is None or not pg.snapshot_path(pageid, self.snap_dir).exists():
            return None
        return pg.load_snapshot(pageid, self.snap_dir)

    def _analysis(self, snap: dict, log: es.Log | None = None) -> dict:
        log = es.read(self.store()) if log is None else log
        pageid = snap["pageid"]
        return views.page_analysis(snap, log, self.queue.pending_on(pageid), self.queue.stopped.get(pageid))

    def page(self, title: str) -> dict | None:
        snap = self._snapshot(title)
        return None if snap is None else self._analysis(snap)

    def watchlist_rows(self) -> list[dict]:
        index, log = titles.load(self.pages_dir), es.read(self.store())
        pageids = {index[t] for t in map(titles.normalise, self.watchlist) if t in index}
        snaps = [pg.load_snapshot(p, self.snap_dir) for p in sorted(pageids)]
        return views.watchlist_rows([(s, self._analysis(s, log)) for s in snaps])

    def _revision(self, revid: int) -> dict | None:
        """The revision's fields from its snapshot, else from the live feed's fetch."""
        if revid in self.revs:
            snap = pg.load_snapshot(self.revs[revid], self.snap_dir)
            rev = next((r for r in snap["revisions"] if r["revid"] == revid), None)
            if rev:
                return rev | {"title": snap["title"]}
        return self.live.revs.get(revid)

    def edit(self, revid: int) -> dict | None:
        """The Edit; None when the revid is in no snapshot, not from the live feed, and not logged."""
        log = es.read(self.store())
        rev = self._revision(revid)
        if rev is None and revid not in log.extracted:
            return None
        rev = rev or {"revid": revid}
        return views.edit(rev, rev.get("title"), log.extracted.get(revid), log.classified.get(revid))

    def cached_brief(self, title: str) -> dict | None:
        pageid = titles.lookup(self.pages_dir, title)
        path = self.brief_dir / f"{pageid}.json"
        return json.loads(path.read_text()) if pageid is not None and path.exists() else None

    def status(self) -> dict:
        path = self.data_dir / "mubit_status.json"
        console = json.loads(path.read_text()) if path.exists() else {}
        s, spent = self.session, self.session.sent.total()
        mubit = {"spent": self.limiter.spent, "remaining": self.limiter.remaining}
        wiki = {"spent": spent, "remaining": max(0, s.budget - spent), "reserve": s.reserve}
        return views.status(provenance.read_calls(self.store()), console, mubit, wiki)

    def cost(self) -> dict:
        return views.cost(provenance.read_calls(self.store()))

    # ---- POSTs: Wikipedia (refresh) and Mubit (classify, brief) -----------------

    def _resolve(self, title: str) -> dict | None:
        """The article a title names, redirects followed; None when missing or not in ns 0
        (pages.fetch_window follows redirects but does not look at the namespace)."""
        q = {"action": "query", "prop": "info", "titles": title, "redirects": 1}
        found = self.session.query(q)["query"].get("pages") or [{}]
        p = found[0]
        return p if p.get("pageid") and p.get("ns") == 0 and not p.get("missing") else None

    def _claim(self, pageid: int) -> None:
        with self.lock:
            last = self.refreshed.get(pageid)
            if last is not None and time.monotonic() - last < REFRESH_EVERY_S:
                raise Refused(429, "recent")
            self.refreshed[pageid] = time.monotonic()

    def refresh(self, title: str) -> dict:
        """Ingests or re-ingests one article, at most once per 10 min per pageid. pages.ingest
        writes the snapshot and the index entries only once the whole ingest succeeded, and a
        failed refresh gives the 10 minutes back."""
        title = titles.normalise(title)
        pageid = titles.lookup(self.pages_dir, title)
        if pageid is None:
            page = _fetch(lambda: self._resolve(title))
            if page is None:
                raise Refused(404, "no such article in the main namespace")
            pageid = page["pageid"]
        self._claim(pageid)
        as_of, days = pg.default_as_of(), self.opts.days
        try:
            snaps = _fetch(lambda: pg.ingest(self.session, [title], as_of, days, self.snap_dir))
            if not snaps:
                raise Refused(404, "no such article in the main namespace")
        except Refused:
            with self.lock:
                self.refreshed.pop(pageid, None)
            raise
        snap = snaps[0]
        with self.lock:
            self.revs.update({r["revid"]: snap["pageid"] for r in snap["revisions"]})
        return self._analysis(snap)

    def classify(self, title: str) -> dict:
        """Queues the article's open 30-day edits for the worker (ClassifyQueue.submit)."""
        snap = self._snapshot(title)
        if snap is None:
            raise Refused(404, "not ingested")
        s = self.session
        if s.sent.total() >= s.budget - s.reserve:
            raise Refused(429, WIKIPEDIA)
        out = self.queue.submit(snap, es.read(self.store()))
        if out is None:
            raise Refused(429, BUDGET)
        return out | {"remaining": self.limiter.remaining}

    def brief(self, title: str) -> dict:
        """The cached brief, generated through Mubit the first time."""
        snap = self._snapshot(title)
        if snap is None:
            raise Refused(404, "not ingested")
        path = self.brief_dir / f"{snap['pageid']}.json"
        with self.brief_lock:
            if path.exists():
                return json.loads(path.read_text())
            if not self.limiter.spendable:
                raise Refused(429, BUDGET)
            analysis, store = self._analysis(snap), self.store()
            caller = Caller(self.client, store, "brief", limiter=self.limiter)
            res = pipeline.brief(self.client, store, analysis, analysis["flags"], self.opts.model, caller)
            if res is None:
                raise Refused(429, BUDGET)
            if res.error:
                raise Refused(502, res.error)
            out = {
                "brief_md": res.extra["brief_md"],
                "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "model_served": res.model_served,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(out))
            return out

    # ---- what the threads run -------------------------------------------------------

    def _texts(self, revids: list[int]) -> dict[int, str]:
        return pg.fetch_wikitext(self.session, revids)

    def _run(self, caller: Caller, revs: list[dict]) -> list[dict]:
        return pipeline.run(caller, revs, self._texts, self.opts.model, log=lambda _m: None)

    def _run_one(self, caller: Caller, job: dict) -> dict | None:
        return next(iter(self._run(caller, [job])), None)
