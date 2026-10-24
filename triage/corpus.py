"""Build a local corpus of English Wikipedia edits for the no-LLM edit classifier.

Network side: sample one-hour windows, list every main-namespace edit in them (bots too, for
the population), link each reverted edit to the edit that undid it, and fetch diffs and
editor info for a sample. Every response is cached under data/corpus/cache, so a re-run makes
no requests. No language model is called; Wikimedia's free revert-risk model is queried only
as a comparison. Pure helpers (windows, linking, sampling) are tested."""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import re
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from .config import DATA_DIR
from .features import parse_compare
from .live import is_ip
from .reconcile import is_reverting
from .wiki import REVERTED_TAG, REVERTING_TAGS, _api, _client, _send, is_content_edit

HOST = "en.wikipedia.org"
COMPARE_URL = "https://en.wikipedia.org/w/rest.php/v1/revision/{old}/compare/{new}"
LIFTWING_URL = "https://api.wikimedia.org/service/lw/inference/v1/models/revertrisk-language-agnostic:predict"
CORPUS_DIR = DATA_DIR / "corpus"
RC_PROPS = "ids|title|timestamp|user|userid|comment|sizes|tags|flags"
# Wikimedia's 2026 limits: a logged-out client with a contact User-Agent gets 200 requests a
# minute across all Wikimedia sites (Lift Wing included), at 3 or fewer concurrent requests.
WORKERS = 3
REQUESTS_PER_MIN = 160
SETTLE_HOURS = 72  # p90 revert delay is ~15 h; three days lets nearly every revert land
TEST_SHARE = 1 / 3  # the latest third of the windows is the test split


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---- pure helpers (tested) -------------------------------------------------


def sample_windows(anchor: date, n: int, first_day: int = 4, last_day: int = 27) -> list[dict]:
    """n one-hour windows spread over days first_day..last_day before `anchor`, oldest first.
    The hour of day steps by 7 (coprime with 24), so 24 windows cover every hour once, and 24
    consecutive days cover every weekday three or four times."""
    span = last_day - first_day
    starts = []
    for i in range(n):
        day = last_day - round(i * span / max(n - 1, 1))
        start = datetime.combine(anchor, datetime.min.time(), UTC) - timedelta(days=day)
        starts.append(start + timedelta(hours=(i * 7) % 24))
    n_test = round(n * TEST_SHARE)
    return [
        {"window": i, "start": iso(s), "end": iso(s + timedelta(hours=1)), "test": i >= n - n_test}
        for i, s in enumerate(sorted(starts))
    ]


def day_chunks(start: datetime, end: datetime) -> list[tuple[str, str]]:
    """[start, end) cut at midnight UTC, so each listing chain is cacheable on its own."""
    out = []
    t = start
    while t < end:
        nxt = min(datetime.combine(t.date() + timedelta(days=1), datetime.min.time(), UTC), end)
        out.append((iso(t), iso(nxt)))
        t = nxt
    return out


def is_human_content(rc: dict) -> bool:
    """The edits the desk sees: human, not a page creation, not itself a revert."""
    return not rc.get("bot") and is_content_edit(rc) and not is_reverting(rc)


def _pick_reverter(edit: dict, later: list[dict]) -> tuple[dict, str]:
    """Prefer a revert whose summary cites the revision id, then one naming the editor,
    then simply the next reverting edit on the page."""
    cites = re.compile(rf"\b{edit['revid']}\b")
    for r in later:
        if cites.search(r.get("comment") or ""):
            return r, "revid"
    user = edit.get("user") or ""
    for r in later:
        if user and user in (r.get("comment") or ""):
            return r, "user"
    return later[0], "next"


def link_reverts(reverted: Iterable[dict], reverters: Iterable[dict]) -> dict[int, dict]:
    """revid of each reverted edit -> the reverting edit that undid it, with how it was matched
    and the delay. Reverted edits with no later reverting edit on the same page are absent."""
    by_page: dict[int, list[dict]] = defaultdict(list)
    for r in sorted(reverters, key=lambda r: r["revid"]):
        by_page[r["pageid"]].append(r)
    out = {}
    for e in reverted:
        later = [r for r in by_page.get(e["pageid"], []) if r["revid"] > e["revid"]]
        if not later:
            continue
        r, how = _pick_reverter(e, later)
        out[e["revid"]] = {
            "revid": e["revid"],
            "reverter_revid": r["revid"],
            "link": how,
            "delay_s": int((ts(r["timestamp"]) - ts(e["timestamp"])).total_seconds()),
            "reverter_user": r.get("user"),
            "reverter_bot": bool(r.get("bot")),
            "reverter_comment": r.get("comment") or "",
            "reverter_tags": r.get("tags") or [],
        }
    return out


def choose_sample(edits: list[dict], n: int, seed: int = 0) -> tuple[list[dict], dict]:
    """Every reverted human content edit (a random n//2 if there are more) plus a uniform random
    sample of kept ones to fill n. Returns the sample and each class's sampling fraction, so
    metrics can be re-weighted to the natural revert rate."""
    human = [e for e in edits if is_human_content(e)]
    rev = [e for e in human if REVERTED_TAG in e.get("tags", [])]
    keep = [e for e in human if REVERTED_TAG not in e.get("tags", [])]
    rng = random.Random(seed)
    pick_rev = rng.sample(rev, min(len(rev), n // 2))
    pick_keep = rng.sample(keep, min(len(keep), n - len(pick_rev)))
    info = {
        "population": {"revert": len(rev), "keep": len(keep)},
        "sampled": {"revert": len(pick_rev), "keep": len(pick_keep)},
    }
    return pick_rev + pick_keep, info


# ---- cached, counted HTTP --------------------------------------------------


class Session:
    """wiki._client() plus an on-disk gzip cache, a request budget, and a pace of at most
    `per_min` requests a minute shared by every thread (retries included)."""

    def __init__(self, root: Path, budget: int, per_min: float = REQUESTS_PER_MIN):
        self.root = root
        self.budget = budget
        self.interval = 60.0 / per_min
        self.sent: Counter[str] = Counter()
        self.hits = 0
        self._lock = threading.Lock()
        self._next_slot = 0.0
        self.client = _client()
        self.client.event_hooks["request"] = [self._on_request]

    def _on_request(self, request) -> None:
        with self._lock:
            self.sent[request.url.host] += 1
            if self.sent.total() > self.budget:
                raise SystemExit(f"request budget of {self.budget} spent; re-run resumes from the cache")
            now = time.monotonic()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.interval
        time.sleep(slot - now)

    def cached(self, parts: list, fetch: Callable[[], object]):
        key = hashlib.sha1(json.dumps(parts, sort_keys=True).encode()).hexdigest()
        path = self.root / "cache" / key[:2] / f"{key}.json.gz"
        if path.exists():
            with self._lock:
                self.hits += 1
            return json.loads(gzip.decompress(path.read_bytes()))
        value = fetch()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{key}.{threading.get_ident()}.tmp")
        tmp.write_bytes(gzip.compress(json.dumps(value).encode()))
        tmp.replace(path)
        return value

    def query(self, params: dict) -> dict:
        params = {**params, "maxlag": 5}
        return self.cached(["api", params], lambda: _api(self.client, HOST, params))

    def rest(self, url: str) -> dict | None:
        """GET a REST URL; None when the revision is gone (deleted or suppressed)."""

        def fetch():
            r = _send(self.client, "GET", url)
            if r.status_code in (403, 404):
                return None
            r.raise_for_status()
            return r.json()

        return self.cached(["get", url], fetch)


def parallel(fn: Callable, items: list) -> list:
    with ThreadPoolExecutor(WORKERS) as pool:
        return list(pool.map(fn, items))


# ---- network ---------------------------------------------------------------


def list_changes(s: Session, start: str, end: str, **extra) -> list[dict]:
    """Every main-namespace recent change in [start, end), oldest first, bots included."""
    params = {
        "action": "query",
        "list": "recentchanges",
        "rcnamespace": 0,
        "rcprop": RC_PROPS,
        "rclimit": 500,
        "rcdir": "newer",
        "rcstart": start,
        "rcend": end,
        **extra,
    }
    out: list[dict] = []
    while True:
        data = s.query(params)
        out.extend(data["query"]["recentchanges"])
        if "continue" not in data:
            return out
        params = {**params, **data["continue"]}


def list_windows(s: Session, windows: list[dict]) -> list[dict]:
    def one(w):
        rows = list_changes(s, w["start"], w["end"], rctype="edit|new")
        return [{**rc, "window": w["window"], "split": "test" if w["test"] else "train"} for rc in rows]

    return [rc for rows in parallel(one, windows) for rc in rows]


def list_reverters(s: Session, start: datetime, end: datetime) -> list[dict]:
    """Every main-namespace edit tagged as a revert in [start, end), bots included, one listing
    per tag and day."""
    jobs = [(tag, a, b) for tag in sorted(REVERTING_TAGS) for a, b in day_chunks(start, end)]
    chunks = parallel(lambda j: list_changes(s, j[1], j[2], rctag=j[0], rctype="edit"), jobs)
    seen: dict[int, dict] = {}
    for rows in chunks:
        for rc in rows:
            seen[rc["revid"]] = rc
    return list(seen.values())


def fetch_users(s: Session, names: Iterable[str], groups: bool = False) -> dict[str, dict]:
    """Account edit count and registration per user name (and groups, if asked: the only bot
    signal); IP editors have no account. The default request is unchanged, so the corpus
    cache stays valid."""
    todo = sorted({n for n in names if n and not is_ip(n)})
    batches = [todo[i : i + 50] for i in range(0, len(todo), 50)]
    usprop = "editcount|registration|groups" if groups else "editcount|registration"
    params = {"action": "query", "list": "users", "usprop": usprop}
    replies = parallel(lambda b: s.query({**params, "ususers": "|".join(b)}), batches)
    return {
        u["name"]: {"editcount": u.get("editcount"), "registration": u.get("registration")}
        | ({"groups": u.get("groups", [])} if groups else {})
        for data in replies
        for u in data["query"]["users"]
        if "missing" not in u and "invalid" not in u
    }


def fetch_diffs(s: Session, edits: list[dict], log: Callable[[str], None]) -> dict[int, dict | None]:
    done = Counter()

    def one(rc):
        data = s.rest(COMPARE_URL.format(old=rc["old_revid"], new=rc["revid"]))
        done["n"] += 1
        if done["n"] % 1000 == 0:
            log(f"  diffs {done['n']}/{len(edits)} (sent {s.sent.total()})")
        return rc["revid"], (parse_compare(data) if data else None)

    return dict(parallel(one, edits))


class LiftWingUnavailable(Exception):
    """Lift Wing refused (rate limit or outage) even after backing off; stop scoring."""


def liftwing_score(s: Session, revid: int) -> dict:
    """Wikimedia's revertrisk-language-agnostic probability of revert, with its latency.
    Refusals are raised, not cached, so a later run retries them."""

    def fetch():
        t0 = time.monotonic()
        r = _send(s.client, "POST", LIFTWING_URL, json={"rev_id": revid, "lang": "en"})
        latency_ms = int((time.monotonic() - t0) * 1000)
        if r.status_code == 429 or r.status_code >= 500:
            raise LiftWingUnavailable(f"HTTP {r.status_code} for rev {revid}")
        if r.status_code != 200:  # e.g. a deleted revision: a final answer, cached
            return {"p": None, "error": r.status_code, "latency_ms": latency_ms}
        return {"p": r.json()["output"]["probabilities"]["true"], "latency_ms": latency_ms}

    return s.cached(["liftwing", revid], fetch)
