"""Page-level signals over one snapshot (PIPELINE.md section 5): activity and bursts, who edits,
reverts, structure drift and bloat, and the documented risk score.

Pure functions. The pipeline's per-edit results (`edits`: revid -> Edit-shaped dict with
`classification`, `extract`, `measured`) are optional; without them the flag-based fields are
0 or null and the score is marked partial. Weights are documented, not tuned to demo pages."""

from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import UTC, datetime, timedelta

from .wiki import EDITOR_LABELS

CONTROVERSY = re.compile(
    r"controvers|criticism|\blegal (?:issues|proceedings|action|cases?)\b|allegation|scandal|investigation|convict|expenses|suspen|misconduct|lobbying",
    re.I,
)
RECENT_DAYS = 30
NEW_ACCOUNT_DAYS = 30
SPA_MIN_EDITS = 10
SPA_MIN_SHARE = 0.6
BURST_MIN = 3


def _t(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _share(n: int, total: int) -> float:
    return round(n / total, 3) if total else 0.0


# ---- activity and editors ----------------------------------------------------


def edits_per_day(revisions: list[dict], as_of: datetime, days: int) -> list[dict]:
    """[{date, count}] for every calendar day the window touches, zero days included."""
    first = (as_of - timedelta(days=days)).date()
    counts = Counter(_t(r["timestamp"]).date() for r in revisions)
    span = (as_of.date() - first).days + 1
    return [
        {"date": d.isoformat(), "count": counts.get(d, 0)}
        for d in (first + timedelta(days=i) for i in range(span))
    ]


def burst_days(per_day: list[dict]) -> list[str]:
    """Days with more than mean + 2σ edits and at least BURST_MIN."""
    counts = [d["count"] for d in per_day]
    if not counts:
        return []
    cut = statistics.fmean(counts) + 2 * statistics.pstdev(counts)
    return [d["date"] for d in per_day if d["count"] > cut and d["count"] >= BURST_MIN]


def top_share(revisions: list[dict], n: int = 3) -> float:
    """Share of edits by the n most active editors; each IP and temporary name is one editor."""
    counts = Counter(r["user"] for r in revisions)
    return _share(sum(c for _, c in counts.most_common(n)), len(revisions))


def is_spa(edits_here: int, user: dict | None) -> bool:
    share = (user or {}).get("page_share")
    return edits_here >= SPA_MIN_EDITS and share is not None and share >= SPA_MIN_SHARE


def is_new_account(rev: dict, users: dict[str, dict]) -> bool:
    """A registered account less than NEW_ACCOUNT_DAYS old when it made the edit. Temporary
    accounts are always new and are counted apart (temp_share)."""
    reg = (users.get(rev["user"]) or {}).get("registration")
    if rev["editor_kind"] != "registered" or not reg:
        return False
    return _t(rev["timestamp"]) - _t(reg) < timedelta(days=NEW_ACCOUNT_DAYS)


def editors(revisions: list[dict], users: dict[str, dict], as_of: datetime) -> list[dict]:
    """One row per registered account; IP editors in one row, temporary accounts in another."""
    groups: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in revisions:
        kind = r["editor_kind"]
        groups[(r["user"] if kind == "registered" else EDITOR_LABELS[kind], kind)].append(r)
    rows = []
    for (name, kind), revs in groups.items():
        u = users.get(name) if kind == "registered" else None
        reg = (u or {}).get("registration")
        rows.append(
            {
                "name": name,
                "kind": kind,
                "edits": len(revs),
                "share": _share(len(revs), len(revisions)),
                "account_age_days": (as_of - _t(reg)).days if reg else None,
                "edit_count": (u or {}).get("editcount"),
                "spa": is_spa(len(revs), u) if kind == "registered" else None,
                "reverted": sum(r["reverted"] for r in revs),
            }
        )
    return sorted(rows, key=lambda e: (-e["edits"], e["name"]))


# ---- structure -------------------------------------------------------------


def controversy_rank(sections: list[dict]) -> int | None:
    return next((i for i, s in enumerate(sections) if CONTROVERSY.search(s["title"])), None)


def controversy_words(sections: list[dict]) -> int:
    """Words in sections whose title matches, and in the subsections under them."""
    total, inside = 0, None
    for s in sections:
        if inside is not None and s["level"] > inside:
            total += s["words"]
            continue
        inside = s["level"] if CONTROVERSY.search(s["title"]) else None
        if inside is not None:
            total += s["words"]
    return total


def _measure(sections: list[dict] | None) -> tuple[int | None, int | None, int | None]:
    """(words, controversy words, controversy rank); all None when the text is unknown."""
    if sections is None:
        return None, None, None
    return sum(s["words"] for s in sections), controversy_words(sections), controversy_rank(sections)


def structure(snap: dict, negative_words_removed: int | None = None) -> dict:
    """Sections then and now are null when that revision's text is hidden (revision deleted or
    suppressed). With no text now there is nothing to compare, so the then side is not used:
    a hidden text is no evidence that a section went."""
    then = (snap.get("sections_then") or {}).get("sections")
    now = (snap.get("sections_now") or {}).get("sections")
    if now is None:
        then = None
    words_then, cw_then, rank_then = _measure(then)
    words_now, cw_now, rank_now = _measure(now)
    bloat = None
    if words_then:
        bloat = round((words_now - words_then - (cw_now - cw_then)) / words_then, 3)
    return {
        "sections_then": snap.get("sections_then"),
        "sections_now": snap.get("sections_now"),
        "controversy_rank_then": rank_then,
        "controversy_rank_now": rank_now,
        "controversy_words_then": cw_then,
        "controversy_words_now": cw_now,
        "controversy_share_then": _share(cw_then, words_then) if words_then else None,
        "controversy_share_now": _share(cw_now, words_now) if words_now else None,
        "words_then": words_then,
        "words_now": words_now,
        "bloat_index": bloat,
        "negative_words_removed": negative_words_removed,
    }


def rank_move(struct: dict) -> int:
    """How many places the controversy section moved down (negative: up); 0 when absent."""
    then, now = struct["controversy_rank_then"], struct["controversy_rank_now"]
    return now - then if then is not None and now is not None else 0


def controversy_drop(struct: dict) -> float:
    """0..1 for the risk term: three places down is 1. A controversy section present then and
    gone now (deleted or renamed away) is 1; one absent then (new, or never there) is 0."""
    if struct["controversy_rank_then"] is None:
        return 0.0
    if struct["controversy_rank_now"] is None:
        return 1.0
    return min(1, max(0, rank_move(struct)) / 3)


# ---- pipeline-dependent fields and the score -------------------------------


def is_flagged(classification: dict | None) -> bool:
    return bool(classification) and (
        bool(classification.get("review")) or classification.get("severity", 0) >= 2
    )


def negative_words_removed(revisions: list[dict], edits: dict[int, dict] | None) -> int | None:
    """Words removed by edits whose extract says negative information was removed or softened."""
    if not edits:
        return None
    total = 0
    for r in revisions:
        e = edits.get(r["revid"]) or {}
        if (e.get("extract") or {}).get("negative_info") in ("removed", "softened"):
            total += (e.get("measured") or {}).get("words_removed", 0)
    return total


def risk(analytics: dict, struct: dict) -> tuple[float, dict]:
    """risk_score (0-100) and the points each term contributed (PIPELINE.md section 5)."""
    parts = {
        "flagged": 30 * min(1, analytics["flagged_30d"] / 5),
        "concentration": 20 * analytics["top3_share"],
        "controversy_move": 15 * controversy_drop(struct),
        "reverted": 15 * min(1, analytics["reverted_30d"] / 3),
        "new_accounts": 10 * analytics["new_account_share"],
        "spa": 10 * (analytics["spa_count"] > 0),
    }
    parts = {k: round(v, 1) for k, v in parts.items()}
    return round(sum(parts.values()), 1), parts


def recent(snap: dict) -> list[dict]:
    """Revisions in the last RECENT_DAYS before as_of (the badge and *_30d fields)."""
    cut = _t(snap["as_of"]) - timedelta(days=RECENT_DAYS)
    return [r for r in snap["revisions"] if _t(r["timestamp"]) > cut]


def activity(snap: dict, edits: dict[int, dict] | None = None) -> dict:
    """The section 5 fields other than structure."""
    revs, users, last30 = snap["revisions"], snap["users"], recent(snap)
    per_day = edits_per_day(revs, _t(snap["as_of"]), snap["days"])
    by_user = Counter(r["user"] for r in revs)
    registered = {r["user"] for r in revs if r["editor_kind"] == "registered"}
    edits = edits or {}
    return {
        "edits_90d": len(revs),
        "edits_30d": len(last30),
        "edits_per_day": per_day,
        "burst_days": burst_days(per_day),
        "top3_share": top_share(revs),
        "editors_90d": len(by_user),
        # registered accounts only, like the editors table's spa column
        "spa_count": sum(is_spa(by_user[name], users.get(name)) for name in registered),
        "new_account_share": _share(sum(is_new_account(r, users) for r in revs), len(revs)),
        "ip_share": _share(sum(r["editor_kind"] == "anonymous" for r in revs), len(revs)),
        "temp_share": _share(sum(r["editor_kind"] == "temporary" for r in revs), len(revs)),
        "reverted_30d": sum(r["reverted"] for r in last30),
        "reverted_90d": sum(r["reverted"] for r in revs),
        "flagged_30d": sum(is_flagged((edits.get(r["revid"]) or {}).get("classification")) for r in last30),
    }


def analyse(snap: dict, edits: dict[int, dict] | None = None, computed_at: datetime | None = None) -> dict:
    """PageAnalysis (PIPELINE.md section 9) without `flags`, which come from the call log."""
    a = activity(snap, edits)
    struct = structure(snap, negative_words_removed(snap["revisions"], edits))
    score, parts = risk(a, struct)
    edits = edits or {}
    return {
        "title": snap["title"],
        "pageid": snap["pageid"],
        "as_of": snap["as_of"],
        "days": snap["days"],
        "computed_at": (computed_at or datetime.now(UTC)).isoformat(),
        "risk_score": score,
        "risk_partial": any(not (edits.get(r["revid"]) or {}).get("classification") for r in recent(snap)),
        "risk_parts": parts,
        "analytics": a,
        "structure": struct,
        "editors": editors(snap["revisions"], snap["users"], _t(snap["as_of"])),
    }
