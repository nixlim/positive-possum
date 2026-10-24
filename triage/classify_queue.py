"""The classify queue (PIPELINE.md section 9): one FIFO of edit jobs shared by every page and
drained by one worker, spending Mubit calls only down to the live reserve. When the reserve, a
gateway 429 or the Wikipedia budget stops the worker, the leftover jobs are dropped and each page
that lost one is marked with the reason, in memory only."""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable

from . import corpus as cp
from . import edit_state as es
from . import page_analytics as pa
from .caller import Caller, Limiter

JOB_CALLS = 4  # extract, classify and a retry of each: the most one job costs (section 7.4)
BUDGET, WIKIPEDIA = "budget", "wikipedia"  # why jobs were dropped


class ClassifyQueue:
    def __init__(self, limiter: Limiter, run: Callable[[Caller, dict], dict | None]):
        self.limiter, self.run = limiter, run
        self.jobs: queue.Queue = queue.Queue()
        self.pending: dict[int, int] = {}  # revid -> pageid, queued or running
        self.stopped: dict[int, str] = {}  # pageid -> BUDGET | WIKIPEDIA
        self.lock = threading.Lock()

    def pending_on(self, pageid: int) -> frozenset[int]:
        with self.lock:
            return frozenset(r for r, p in self.pending.items() if p == pageid)

    def submit(self, snap: dict, log: es.Log) -> dict | None:
        """Queues the page's open 30-day edits, newest first, as far as the spendable budget
        (`remaining − live_reserve − 4·pending_all`) covers four calls each, and clears the
        page's stop reason; None, with nothing queued, when it covers no job."""
        recent = sorted(pa.recent(snap), key=lambda r: (r["timestamp"], r["revid"]), reverse=True)
        with self.lock:
            spendable = self.limiter.spendable - JOB_CALLS * len(self.pending)
            if spendable < JOB_CALLS:
                return None
            part = es.partition(log, [r["revid"] for r in recent], self.pending)
            open_ = set(part[es.OPEN])
            todo = [r for r in recent if r["revid"] in open_]
            room = spendable // JOB_CALLS
            for r in todo[:room]:
                self.pending[r["revid"]] = snap["pageid"]
                account = snap["users"].get(r.get("user"))
                self.jobs.put({**r, "title": snap["title"], "pageid": snap["pageid"], "account": account})
            self.stopped.pop(snap["pageid"], None)
        return {
            "queued": min(room, len(todo)),
            "already": len(part[es.CLASSIFIED]),
            "errored": len(part[es.ERRORED]),
            "pending": len(part[es.PENDING]),
            "skipped": max(0, len(todo) - room),
        }

    def work(self, caller: Caller) -> None:
        """Runs jobs in order until the None that stop() queues."""
        while (job := self.jobs.get()) is not None:
            try:
                self.do(caller, job)
            finally:
                self.jobs.task_done()
        self.jobs.task_done()

    def do(self, caller: Caller, job: dict) -> None:
        """One job. At the live reserve no call is made and the whole queue is dropped; the
        Caller checks the reserve again before each call, the retry included."""
        stop = BUDGET if self.limiter.spendable <= 0 else None
        if stop is None:
            caller.refused = False
            try:
                result = self.run(caller, job) or {}
                if caller.refused or (result.get("error") or "").startswith("HTTP 429"):
                    stop = BUDGET  # the reserve, or Mubit's cap: every next job would hit it too
            except cp.BudgetSpent:
                stop = WIKIPEDIA
            except (Exception, SystemExit) as e:  # a transient fetch failure: only this job goes
                print(f"classify {job['revid']}: {type(e).__name__}: {e}", flush=True)
        with self.lock:
            self.pending.pop(job["revid"], None)
            if stop:
                self._drop(stop, job["pageid"])

    def _drop(self, reason: str, pageid: int) -> None:
        """Empties the queue (under the lock, so no submit interleaves) and marks every page
        that lost a job, the running job's page included."""
        pageids = {pageid}
        while True:
            try:
                left = self.jobs.get_nowait()
            except queue.Empty:
                break
            self.jobs.task_done()
            if left is None:  # stop() asked the worker to end: keep the request for it
                self.jobs.put(None)
                break
            self.pending.pop(left["revid"], None)
            pageids.add(left["pageid"])
        for p in pageids:
            self.stopped[p] = reason
