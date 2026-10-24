"""Mubit calls under a shared budget and pace (PIPELINE.md sections 7.4 and 9): a Limiter that
several Callers on several threads share, keeping a reserve that only `live` callers may spend,
and a Caller that logs every attempt and retries an unparseable reply once."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime

from anthropic import Anthropic

from .llm import CallResult, send
from .store import Store

LIVE = "live"  # the one kind that may spend the limiter's reserve


class Limiter:
    """A call budget and a pace that several Callers, on several threads, can share: the count
    and the next free slot are taken under one lock, and the wait for the slot happens outside it.
    The last `reserve` calls are only given to callers that may use the reserve."""

    def __init__(self, max_calls: int, rate: float = 0.0, reserve: int = 0):
        self.max_calls, self.rate, self.reserve = max_calls, rate, reserve
        self._spent = 0
        self._next = float("-inf")
        self._lock = threading.Lock()

    def take(self, use_reserve: bool = False) -> bool:
        """One call's worth of budget, after waiting for its slot; False when spent (down to the
        reserve, unless `use_reserve`)."""
        with self._lock:
            if self._spent >= self.max_calls - (0 if use_reserve else self.reserve):
                return False
            self._spent += 1
            slot = max(time.monotonic(), self._next)
            self._next = slot + self.rate
        time.sleep(max(0.0, slot - time.monotonic()))
        return True

    @property
    def spent(self) -> int:
        return self._spent

    @property
    def remaining(self) -> int:
        return max(0, self.max_calls - self._spent)

    @property
    def spendable(self) -> int:
        """Calls left to callers that may not spend the reserve."""
        return max(0, self.remaining - self.reserve)


class Caller:
    """Sends through Mubit within a call budget and a pace, logging every attempt. One run_id
    per Caller; the budget and pace are its own unless it is given a shared Limiter. `refused`
    is set when the limiter turned an attempt down (the owner resets it per job)."""

    def __init__(
        self,
        client: Anthropic,
        store: Store,
        kind: str,
        max_calls: int = 0,
        rate: float = 0.0,
        limiter: Limiter | None = None,
    ):
        self.client, self.store, self.kind = client, store, kind
        self.run_id = f"{kind}-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S')}"
        self.limiter = limiter or Limiter(max_calls, rate)
        self.use_reserve = kind == LIVE
        self.refused = False

    @property
    def left(self) -> int:
        return self.limiter.remaining

    def call(
        self, step: str, item_id: int, req: dict, parse: Callable, extra: dict | None = None
    ) -> tuple[CallResult, int] | None:
        """The last logged result and its row id, retried once if unparseable; None when the
        budget was spent before the first attempt. The retry's row names the row it retries
        (`retry_of`), which is how a final error is told from a first unparseable reply."""
        got = None
        for _ in range(2):
            if not self.limiter.take(self.use_reserve):
                self.refused = True
                return got
            res = send(self.client, req, parse)
            res.extra.update(extra or {})
            if got is not None:
                res.extra["retry_of"] = got[1]
            got = res, self.store.log_call(self.run_id, self.kind, step, item_id, res)
            if not (res.error or "").startswith("unparseable reply"):
                break
        return got
