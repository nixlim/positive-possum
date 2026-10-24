"""Assemble the edit corpus from the network steps in `triage.corpus` and write it to
data/corpus. The anchor date and snapshot time are fixed by the first run and kept in the
manifest, so later runs ask the cache the same questions and make no requests."""

from __future__ import annotations

import gzip
import json
import random
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from . import corpus as cp
from .store import Store
from .wiki import REVERTED_TAG

LIVE_LEAD_MIN = 30  # the desk judges an edit seconds after it is saved; list a margin before


def _write_jsonl(path: Path, rows) -> None:
    with gzip.open(path, "wt") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _read_jsonl(path: Path) -> list[dict]:
    with gzip.open(path, "rt") as f:
        return [json.loads(line) for line in f]


def live_calls(store: Store) -> list[dict]:
    """The live desk's Sonnet verdicts already logged in data/calls.sqlite (no new calls)."""
    return [c for c in store.calls_since("enwiki", ("live",), "") if not c["error"]]


def live_edits(s: cp.Session, calls: list[dict]) -> list[dict]:
    """Recent-changes rows of the edits the live desk judged."""
    if not calls:
        return []
    wanted = {int(c["issue"]) for c in calls}
    start = cp.ts(min(c["ts"] for c in calls)[:19] + "Z") - timedelta(minutes=LIVE_LEAD_MIN)
    end = cp.ts(max(c["ts"] for c in calls)[:19] + "Z") + timedelta(minutes=1)
    rows = cp.list_changes(s, cp.iso(start), cp.iso(end), rctype="edit")
    return [{**rc, "window": -1, "split": "live"} for rc in rows if rc["revid"] in wanted]


def _liftwing(s: cp.Session, revids: list[int], log: Callable[[str], None]) -> dict[int, dict]:
    out = {}
    for revid in revids:
        try:
            out[revid] = cp.liftwing_score(s, revid)
        except cp.LiftWingUnavailable as e:
            log(f"  Lift Wing stopped after {len(out)} scores: {e}")
            break
    return out


def _fixed_times(root: Path, anchor: date | None) -> tuple[date, str]:
    path = root / "manifest.json"
    prior = json.loads(path.read_text()) if path.exists() else {}
    now = datetime.now(UTC)
    anchor = anchor or (date.fromisoformat(prior["anchor"]) if "anchor" in prior else now.date())
    snapshot = prior.get("snapshot") or cp.iso(now)
    path.write_text(json.dumps({**prior, "anchor": anchor.isoformat(), "snapshot": snapshot}, indent=1))
    return anchor, snapshot


@dataclass(frozen=True)
class Spec:
    """What to collect: `windows` one-hour windows, a diff sample of `diffs` edits, at most
    `budget` HTTP requests, `liftwing` test edits scored by Wikimedia's model."""

    windows: int = 24
    diffs: int = 10_000
    budget: int = 15_000
    liftwing: int = 400
    seed: int = 0
    anchor: date | None = None


def build(spec: Spec, root: Path = cp.CORPUS_DIR, log: Callable[[str], None] = print) -> dict:
    root.mkdir(parents=True, exist_ok=True)
    anchor, snapshot = _fixed_times(root, spec.anchor)
    s = cp.Session(root, spec.budget)
    wins = cp.sample_windows(anchor, spec.windows)
    log(f"listing {len(wins)} one-hour windows (anchor {anchor}, snapshot {snapshot})")
    edits = cp.list_windows(s, wins)
    calls = live_calls(Store())
    live = live_edits(s, calls)
    log(f"  {len(edits)} window edits, {len(live)} live-desk edits; listing reverting edits")
    reverters = cp.list_reverters(s, cp.ts(wins[0]["start"]), cp.ts(snapshot))
    reverted = [e for e in edits + live if REVERTED_TAG in (e.get("tags") or [])]
    links = cp.link_reverts(reverted, reverters)
    log(f"  {len(reverters)} reverting edits; linked {len(links)}/{len(reverted)} reverted edits")

    sample, sampling = cp.choose_sample(edits, spec.diffs, spec.seed)
    live_sample = [e for e in live if cp.is_human_content(e)]
    log(f"fetching {len(sample)} sample + {len(live_sample)} live diffs")
    got = cp.fetch_diffs(s, sample + live_sample, log)
    users = cp.fetch_users(s, [e.get("user") for e in sample + live_sample])  # hidden names are absent

    test = sorted(e["revid"] for e in sample if e["split"] == "test" and got.get(e["revid"]))
    lw_ids = sorted(random.Random(spec.seed).sample(test, min(spec.liftwing, len(test))))
    log(f"scoring {len(lw_ids)} test edits with Lift Wing revertrisk-language-agnostic")
    lw = _liftwing(s, lw_ids, log)

    _write_jsonl(root / "edits.jsonl.gz", edits + live)
    _write_jsonl(root / "links.jsonl.gz", links.values())
    _write_jsonl(root / "diffs.jsonl.gz", ({"revid": k, **v} for k, v in got.items() if v))
    (root / "users.json.gz").write_bytes(gzip.compress(json.dumps(users).encode()))
    (root / "liftwing.json").write_text(json.dumps({str(k): v for k, v in lw.items()}))
    (root / "live_calls.json").write_text(json.dumps(calls))
    manifest = {
        "anchor": anchor.isoformat(),
        "snapshot": snapshot,
        "windows": wins,
        "settle_hours": cp.SETTLE_HOURS,
        "sampling": sampling,
        "seed": spec.seed,
        "counts": {
            "edits_listed": len(edits),
            "live_edits": len(live),
            "reverted": len(reverted),
            "linked": len(links),
            "reverters_listed": len(reverters),
            "diffs_requested": len(got),
            "diffs_missing": sum(v is None for v in got.values()),
            "users": len(users),
            "liftwing": len(lw),
        },
        "requests_sent": dict(s.sent),
        "cache_hits": s.hits,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def load(root: Path = cp.CORPUS_DIR) -> dict:
    """Everything `build` wrote, as plain Python objects."""
    return {
        "manifest": json.loads((root / "manifest.json").read_text()),
        "edits": _read_jsonl(root / "edits.jsonl.gz"),
        "links": {r["revid"]: r for r in _read_jsonl(root / "links.jsonl.gz")},
        "diffs": {r["revid"]: r for r in _read_jsonl(root / "diffs.jsonl.gz")},
        "users": json.loads(gzip.decompress((root / "users.json.gz").read_bytes())),
        "liftwing": {int(k): v for k, v in json.loads((root / "liftwing.json").read_text()).items()},
        "live_calls": json.loads((root / "live_calls.json").read_text()),
    }
