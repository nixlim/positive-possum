"""The patrol desk engine: a reader thread follows the Wikimedia stream, a worker
thread sends one edit at a time through Mubit, and verdict() runs the correction
loop. Every call is logged to the store like any eval call; the dashboard is a
view over that log plus an in-memory ring of recent cards."""

from __future__ import annotations

import asyncio
import contextlib
import json
import queue
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from . import guidelines as gl
from .config import DATA_DIR, MODEL, RepoConfig
from .github import load_dataset
from .live import fetch_item, is_owned, iter_events, summarize
from .llm import CallResult, classify, client
from .reconcile import outcome_stats, reconcile
from .store import Store
from .wiki import LABELS, USER_AGENT

WINDOW_HOURS = 6  # the stats window
MAX_CARDS = 150
QUEUE_DEPTH = 2  # edits waiting for the worker; the stream is faster than our call rate
RECONCILE_EVERY_S = 600  # how often to ask Wikipedia what happened to our live verdicts


def card_from(item: dict, res: CallResult, model: str) -> dict:
    body = item["body"]
    diff = body.split("Diff:\n", 1)[1] if "Diff:\n" in body else body
    return {
        "number": item["number"],
        "title": item["title"],
        "url": item["url"],
        "user": item["user"],
        "editor": item["editor"],
        "comment": item["comment"],
        "diff": diff,
        "verdict": res.labels[0] if res.labels else None,
        "reason": res.extra.get("reason"),
        "confidence": res.extra.get("confidence"),
        "served": res.model_served,
        "owned": is_owned(res.model_served, model),
        "latency_ms": res.latency_ms,
        "tokens": (res.prompt_tokens or 0) + (res.completion_tokens or 0),
        "request_id": res.request_id,
        "error": res.error,
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "correction": None,
        "outcome": None,
    }


class Patrol:
    def __init__(
        self, cfg: RepoConfig, model: str = MODEL, min_interval_s: float = 2.0, data_dir: Path = DATA_DIR
    ):
        self.cfg, self.model, self.min_interval, self.data_dir = cfg, model, min_interval_s, data_dir
        self.labels = LABELS
        self.cards: OrderedDict[int, dict] = OrderedDict()
        self.items: dict[int, dict] = {}
        self.learning: list[dict] = []
        self.lock = threading.Lock()
        self.queue: queue.Queue[dict] = queue.Queue(maxsize=QUEUE_DEPTH)
        self.subscribers: list[asyncio.Queue] = []
        self.loop: asyncio.AbstractEventLoop | None = None
        self.stopping = threading.Event()
        self.dropped = 0
        self.client = client()
        self.status_path = data_dir / cfg.key / "mubit_status.json"
        self.status = (
            json.loads(self.status_path.read_text())
            if self.status_path.exists()
            else {"text": "", "ts": None}
        )

    # ---- lifecycle ----------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        threading.Thread(target=self._reader, name="patrol-reader", daemon=True).start()
        threading.Thread(target=self._worker, name="patrol-worker", daemon=True).start()
        threading.Thread(target=self._reconciler, name="patrol-reconciler", daemon=True).start()

    def stop(self) -> None:
        self.stopping.set()

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        if q in self.subscribers:
            self.subscribers.remove(q)

    def _broadcast(self, kind: str, data: dict) -> None:
        if not self.loop:
            return
        for q in list(self.subscribers):
            self.loop.call_soon_threadsafe(q.put_nowait, (kind, data))

    # ---- threads ------------------------------------------------------------

    def _reader(self) -> None:
        with httpx.Client(headers={"User-Agent": USER_AGENT}) as http:
            while not self.stopping.is_set():
                try:
                    for event in iter_events(http, self.cfg.full_name):
                        if self.stopping.is_set():
                            return
                        try:
                            self.queue.put_nowait(event)
                        except queue.Full:
                            self.dropped += 1  # the worker is busy; the stream has plenty more
                except Exception as e:  # stream dropped: EventStreams asks clients to reconnect
                    self._broadcast("log", {"text": f"stream reconnecting: {type(e).__name__}: {e}"[:200]})
                    time.sleep(3)

    def _worker(self) -> None:
        store = Store(self.data_dir / "calls.sqlite")
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30) as http:
            while not self.stopping.is_set():
                try:
                    event = self.queue.get(timeout=1)
                except queue.Empty:
                    continue
                t0 = time.monotonic()
                try:
                    self._handle(store, http, event)
                except Exception as e:  # one bad edit must not stop the desk
                    self._broadcast("log", {"text": f"edit skipped: {type(e).__name__}: {e}"[:200]})
                time.sleep(max(0.0, self.min_interval - (time.monotonic() - t0)))

    def _reconciler(self) -> None:
        store = Store(self.data_dir / "calls.sqlite")
        with httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=30) as http:
            while not self.stopping.wait(RECONCILE_EVERY_S):
                with self.lock:
                    desk = set(self.cards)
                try:
                    for written in reconcile(store, self.cfg, http, desk=desk):
                        self.apply_outcomes(written)
                # Wikipedia API hiccup (wiki._api raises SystemExit on an error payload): try again
                except (Exception, SystemExit) as e:
                    self._broadcast("log", {"text": f"reconcile skipped: {type(e).__name__}: {e}"[:200]})

    def apply_outcomes(self, outcomes: list[dict]) -> None:
        """Mark cards with Wikipedia's decision; a disagreement with a card still on the desk
        is a correction from Wikipedia itself and runs the same loop as a patroller's click.
        The outcomes are already in the store, so one failure must not skip the rest."""
        for o in outcomes:
            try:
                self._apply_outcome(o)
            except Exception as e:
                self._broadcast("log", {"text": f"outcome {o['issue']}: {type(e).__name__}: {e}"[:200]})
        if outcomes:
            self._broadcast("stats", self.stats())

    def _apply_outcome(self, o: dict) -> None:
        with self.lock:
            card = self.cards.get(o["issue"])
            if not card:
                return  # left the desk: the outcome only feeds the stats
            card["outcome"] = {k: o[k] for k in ("truth", "reverted_at", "reverted_by", "lead_s")}
            # "gone" (revision no longer visible) is not a revert we can teach from.
            correct = (
                o["truth"] in ("keep", "revert")
                and card["verdict"] != o["truth"]
                and card["correction"] is None
            )
        if correct:
            with contextlib.suppress(KeyError, ValueError):  # left the desk, or a patroller won
                self.verdict(o["issue"], o["truth"], source="wikipedia")
                return  # _record broadcast the card
        self._broadcast("card", card)

    def _handle(self, store: Store, http: httpx.Client, event: dict) -> None:
        item = fetch_item(http, event, self.cfg.body_max_chars)
        if item is None:
            return
        res = classify(
            self.client,
            self.cfg.full_name,
            self.labels,
            item,
            self._guidelines(),
            model=self.model,
            kind="wiki",
        )
        store.log_call(f"live-{self.cfg.key}", "live", self.cfg.key, item["number"], res)
        card = card_from(item, res, self.model)
        with self.lock:
            self.items[item["number"]] = item
            self.cards[item["number"]] = card
            while len(self.cards) > MAX_CARDS:
                old, _ = self.cards.popitem(last=False)
                self.items.pop(old, None)
        self._broadcast("card", card)
        self._broadcast("stats", self.stats(store))

    # ---- corrections --------------------------------------------------------

    def _guidelines(self) -> str:
        eval_issues: set[int] = set()
        if (self.data_dir / self.cfg.key / "issues.jsonl").exists():
            _, rows = load_dataset(self.cfg, self.data_dir)
            eval_issues = {r["number"] for r in rows if r["split"] == "eval"}
        return gl.load(self.cfg.key, eval_issues, self.data_dir)

    def verdict(self, revid: int, verdict: str, source: str = "patroller") -> dict:
        """A patroller's click (or, with source="wikipedia", what the wiki did next).
        Agreement is just recorded. Disagreement appends a guideline line and re-sends the
        same edit in the same Mubit session, so the recorded reply becomes the corrected
        answer: that is the only way a correction reaches Mubit.
        Raises KeyError when the edit has left the desk, ValueError when it already has a
        verdict (a double click or a second tab must not write a second guideline)."""
        now = datetime.now(UTC).isoformat(timespec="seconds")
        with self.lock:
            card, item = self.cards.get(revid), self.items.get(revid)
            if not card or not item:
                raise KeyError(revid)
            if card["correction"] is not None:
                raise ValueError(revid)
            old = card["verdict"]
            # Claimed under the lock; the disagreement branch fills in the re-send below.
            card["correction"] = {"verdict": verdict, "changed": old != verdict, "source": source, "ts": now}
        if old == verdict:
            event = {
                "type": "confirmed",
                "number": revid,
                "title": card["title"],
                "verdict": verdict,
                "source": source,
                "ts": now,
            }
            return self._record(card, event)
        try:
            line = gl.add_correction(self.cfg.key, item, [old] if old else [], [verdict], self.data_dir)
        except Exception:
            # Nothing was written: release the claim so a retry can record the correction.
            with self.lock:
                card["correction"] = None
            raise
        guide = self._guidelines()
        res = classify(
            self.client, self.cfg.full_name, self.labels, item, guide, model=self.model, kind="wiki"
        )
        Store(self.data_dir / "calls.sqlite").log_call(
            f"correct-{self.cfg.key}-{revid}", "correct", self.cfg.key, revid, res, truth=[verdict]
        )
        resent = res.labels[0] if res.labels else None
        card["correction"] = {
            "verdict": verdict,
            "changed": True,
            "resent": resent,
            "served": res.model_served,
            "source": source,
            "ts": now,
        }
        event = {
            "type": "correction",
            "number": revid,
            "title": card["title"],
            "source": source,
            "old": old,
            "new": verdict,
            "resent": resent,
            "agrees": resent == verdict,
            "served": res.model_served,
            "request_id": res.request_id,
            "guideline": line,
            "guidelines_total": guide.count("\n"),
            "ts": now,
        }
        return self._record(card, event)

    def _record(self, card: dict, event: dict) -> dict:
        with self.lock:
            self.learning.insert(0, event)
            del self.learning[50:]
        self._broadcast("card", card)
        self._broadcast("learning", event)
        return event

    # ---- views --------------------------------------------------------------

    def set_status(self, text: str) -> dict:
        self.status = {"text": text.strip()[:300], "ts": datetime.now(UTC).isoformat(timespec="seconds")}
        self.status_path.parent.mkdir(parents=True, exist_ok=True)
        self.status_path.write_text(json.dumps(self.status))
        self._broadcast("status", self.status)
        return self.status

    def stats(self, store: Store | None = None) -> dict:
        store = store or Store(self.data_dir / "calls.sqlite")
        since = (datetime.now(UTC) - timedelta(hours=WINDOW_HOURS)).isoformat(timespec="seconds")
        s = summarize(store.calls_since(self.cfg.key, ("live", "correct"), since), self.model)
        s["window_hours"] = WINDOW_HOURS
        s["dropped"] = self.dropped
        s["guidelines"] = self._guidelines().count("\n")
        s["wikipedia"] = outcome_stats(store.outcomes(self.cfg.key))
        return s

    def snapshot(self) -> dict:
        with self.lock:
            cards = list(self.cards.values())[-60:]
            learning = list(self.learning)
        return {
            "repo": self.cfg.full_name,
            "model": self.model,
            "cards": cards,
            "learning": learning,
            "stats": self.stats(),
            "status": self.status,
        }
