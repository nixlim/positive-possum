"""The live feed (PIPELINE.md section 9): watched edits from the Wikimedia stream, each sent
through extract then classify once in the API process and broadcast to every SSE client. Its diff
fetches may spend the Wikipedia live reserve and its calls the Mubit one. Whatever fails, the
edit still goes out, with `stopped` naming the budget that held it back."""

from __future__ import annotations

import asyncio
import queue
from collections import OrderedDict, deque
from collections.abc import Callable

import httpx

from . import corpus as cp
from . import live, pipeline
from . import watch_views as views
from .caller import Caller, Limiter
from .classify_queue import BUDGET, WIKIPEDIA

HOST = "en.wikipedia.org"
LIVE_KEPT = 50  # recent live events a new SSE client is sent first
QUEUE_MAX = 100
LIVE_REVS_KEPT = 2000  # live revisions GET /api/edit can look up; older ones fall out


class LiveFeed:
    def __init__(
        self,
        session: cp.Session,
        limiter: Limiter,
        run: Callable[[Caller, list[dict]], list[dict]],
        watched: set[str],
        http_factory: Callable[[], httpx.Client],
    ):
        self.session, self.limiter, self.run, self.watched = session, limiter, run, watched
        self.http_factory = http_factory
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_MAX)
        self.revs: OrderedDict[int, dict] = OrderedDict()  # revid -> its fetched fields, newest last
        self.recent: deque = deque(maxlen=LIVE_KEPT)
        self.dropped = 0
        self.subscribers: list[asyncio.Queue] = []
        self.loop: asyncio.AbstractEventLoop | None = None

    # ---- broadcast (the patrol desk's pattern; server._feed reads these) -------

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)

    def snapshot(self) -> dict:
        return {"edits": list(self.recent)}

    def _broadcast(self, kind: str, data: dict) -> None:
        if not self.loop:
            return
        for q in list(self.subscribers):
            self.loop.call_soon_threadsafe(q.put_nowait, (kind, data))

    # ---- threads -------------------------------------------------------------

    def read_stream(self, http: httpx.Client, stopping) -> None:
        """One stream connection's watched edits onto the queue (live.iter_events keeps only
        enwiki, main-namespace, non-bot edits)."""
        for event in live.iter_events(http, HOST):
            if stopping.is_set():
                return
            if event["title"] in self.watched:
                try:
                    self.queue.put_nowait(event)
                except queue.Full:
                    self.dropped += 1

    def reader(self, stopping) -> None:
        with self.http_factory() as http:
            while not stopping.is_set():
                try:
                    self.read_stream(http, stopping)
                except Exception as e:  # stream dropped: EventStreams asks clients to reconnect
                    print(f"live stream: {type(e).__name__}: {e}; reconnecting", flush=True)
                stopping.wait(3)

    def handle(self, event: dict, caller: Caller) -> dict:
        """Extract then classify one live edit inside the Wikipedia reserve, once for every
        client. `stopped` is "budget" when Mubit refused a call, "wikipedia" when the request
        budget is spent; a transient fetch failure sends it out unclassified with neither."""
        rc, result, stopped = live.rc_from_event(event), None, None
        if not self.limiter.remaining:
            stopped = BUDGET
        else:
            caller.refused = False
            try:
                with self.session.using_reserve():
                    revs = pipeline.fetch_revisions(self.session, [rc["revid"]])
                    self.revs.update({r["revid"]: r for r in revs})
                    while len(self.revs) > LIVE_REVS_KEPT:
                        self.revs.popitem(last=False)
                    result = next(iter(self.run(caller, revs)), None)
            except cp.BudgetSpent:
                stopped = WIKIPEDIA
            except (Exception, SystemExit) as e:  # a MediaWiki API error is a plain SystemExit
                print(f"live {rc['revid']}: {type(e).__name__}: {e}", flush=True)
            if caller.refused:
                stopped = BUDGET
        ev = views.live_event(rc, result, stopped)
        self.recent.appendleft(ev)
        self._broadcast("edit", ev)
        return ev

    def work(self, caller: Caller) -> None:
        """Handles edits in order until the None that Service.stop() queues."""
        while (event := self.queue.get()) is not None:
            try:
                self.handle(event, caller)
            except (Exception, SystemExit) as e:
                print(f"live {event.get('title')}: {type(e).__name__}: {e}", flush=True)
            finally:
                self.queue.task_done()
