"""Reconcile live verdicts with what Wikipedia did next. A live edit's truth is
unknown when we answer it; hours later it is either tagged mw-reverted (truth:
revert, and the reverting revision's timestamp gives our lead) or still standing
(truth: keep). Pure functions except the two that query the MediaWiki API."""

from __future__ import annotations

import statistics
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import httpx

from .config import RepoConfig
from .live import REVERTING_SUMMARY
from .store import Store
from .wiki import CONTENT_BATCH, MIN_AGE_HOURS, REVERTED_TAG, REVERTING_TAGS, _api

SETTLE_HOURS = MIN_AGE_HOURS  # an edit still standing after this long counts as kept


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def check_revisions(client: httpx.Client, host: str, revids: list[int]) -> dict[int, dict]:
    """revid -> {title, tags, timestamp} for every revision that still exists."""
    out: dict[int, dict] = {}
    for i in range(0, len(revids), CONTENT_BATCH):
        batch = revids[i : i + CONTENT_BATCH]
        q = _api(
            client,
            host,
            {
                "action": "query",
                "prop": "revisions",
                "revids": "|".join(map(str, batch)),
                "rvprop": "ids|timestamp|tags",
            },
        )
        for page in q.get("query", {}).get("pages", []):
            for rev in page.get("revisions", []):
                out[rev["revid"]] = {
                    "title": page["title"],
                    "tags": rev.get("tags", []),
                    "timestamp": rev["timestamp"],
                }
    return out


def is_reverting(rev: dict) -> bool:
    return bool(set(rev.get("tags", [])) & REVERTING_TAGS) or bool(
        REVERTING_SUMMARY.match(rev.get("comment") or "")
    )


def find_revert(client: httpx.Client, host: str, title: str, revid: int, since: str) -> dict | None:
    """The first reverting revision on the page after ours: {revid, timestamp, user} or None.
    A heuristic: that revision may undo a different, later edit, in which case the revert of
    ours came later still, so the lead it gives is a lower bound. Only the next 50 revisions
    are read; a page busier than that gives no lead."""
    q = _api(
        client,
        host,
        {
            "action": "query",
            "prop": "revisions",
            "titles": title,
            "rvdir": "newer",
            "rvstart": since,
            "rvprop": "ids|timestamp|tags|comment|user",
            "rvlimit": 50,
        },
    )
    for page in q.get("query", {}).get("pages", []):
        for rev in page.get("revisions", []):
            if rev["revid"] > revid and is_reverting(rev):
                return {"revid": rev["revid"], "timestamp": rev["timestamp"], "user": rev.get("user")}
    return None


def outcome(call: dict, rev: dict | None, revert: dict | None, now: datetime) -> dict | None:
    """What Wikipedia decided about a live call, or None while the truth is still unsettled."""
    verdict_at = _ts(call["ts"])
    base = {
        "issue": call["issue"],
        "verdict_ts": call["ts"],
        "verdict": call["labels"][0] if call["labels"] else None,
        "reverted_at": None,
        "reverted_by": None,
        "revert_revid": None,
        "lead_s": None,
    }
    if rev is None:  # not returned (page deleted, revision hidden, partial reply): not a known revert
        return {**base, "truth": "gone"}
    if REVERTED_TAG in rev["tags"]:
        o = {**base, "truth": "revert"}
        if revert:
            at = _ts(revert["timestamp"])
            o.update(
                reverted_at=at.isoformat(timespec="seconds"),
                reverted_by=revert["user"],
                revert_revid=revert["revid"],
            )
            o["lead_s"] = int((at - verdict_at).total_seconds())
        return o
    if now - verdict_at >= timedelta(hours=SETTLE_HOURS):
        return {**base, "truth": "keep"}
    return None


def reconcile(
    store: Store,
    cfg: RepoConfig,
    client: httpx.Client,
    now: datetime | None = None,
    desk: set[int] | None = None,
) -> Iterator[list[dict]]:
    """Settle live calls without an outcome yet, one API batch at a time, yielding the outcomes
    written per batch so an API error loses only the batch in flight. With `desk` (revids still
    shown live) only those and calls old enough to settle are queried; the rest wait until they
    are SETTLE_HOURS old, so a long-running desk does not re-query hours of calls every pass."""
    now = now or datetime.now(UTC)
    pending = store.pending_outcomes(cfg.key)
    if desk is not None:
        settle = timedelta(hours=SETTLE_HOURS)
        pending = [c for c in pending if c["issue"] in desk or now - _ts(c["ts"]) >= settle]
    for i in range(0, len(pending), CONTENT_BATCH):
        batch = pending[i : i + CONTENT_BATCH]
        revs = check_revisions(client, cfg.full_name, [c["issue"] for c in batch])
        written = []
        for call in batch:
            rev = revs.get(call["issue"])
            revert = None
            if rev and REVERTED_TAG in rev["tags"]:
                revert = find_revert(client, cfg.full_name, rev["title"], call["issue"], rev["timestamp"])
            o = outcome(call, rev, revert, now)
            if o:
                store.log_outcome(cfg.key, o)
                written.append(o)
        yield written


def outcome_stats(outcomes: list[dict]) -> dict:
    """Precision of our revert flags against Wikipedia, what we missed, and our lead on hits.
    Revisions that are gone have no known truth: counted apart, not scored. A revert we did not
    flag (said keep, or gave no label) is a miss. The lead is over every hit with a known lead,
    negative where Wikipedia reverted first; it is a lower bound (see find_revert)."""
    known = [o for o in outcomes if o["truth"] != "gone"]
    flagged = [o for o in known if o["verdict"] == "revert"]
    hits = [o for o in flagged if o["truth"] == "revert"]
    missed = [o for o in known if o["verdict"] != "revert" and o["truth"] == "revert"]
    leads = [o["lead_s"] for o in hits if o["lead_s"] is not None]
    reverted = len(hits) + len(missed)
    return {
        "settled": len(known),
        "gone": len(outcomes) - len(known),
        "flagged": len(flagged),
        "hits": len(hits),
        "false_alarms": len(flagged) - len(hits),
        "missed": len(missed),
        "precision": round(len(hits) / len(flagged), 3) if flagged else None,
        "recall": round(len(hits) / reverted, 3) if reverted else None,
        "ahead": sum(1 for s in leads if s > 0),
        "behind": sum(1 for s in leads if s < 0),
        "median_lead_min": round(statistics.median(leads) / 60, 1) if leads else None,
        "max_lead_min": round(max(leads) / 60, 1) if leads else None,
    }
