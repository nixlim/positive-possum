"""Rank watchlist pages for the demo (PIPELINE.md section 6) on deterministic signals only: the
risk score without model output, plus bloat, a controversy section moving down, reverts and
edit bursts. A human picks the demo pages from the list; nothing here calls a model."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .page_analytics import controversy_drop, rank_move
from .pages import PAGES_DIR

# Points added to risk_score, documented like the risk weights and not tuned to any page.
EXTRA_WEIGHTS = {"bloat": 10, "controversy_drop": 10, "reverted_90d": 10, "bursts": 5}
REVERTED_FULL = 5  # reverted_90d earning the full points
BURSTS_FULL = 3
BLOAT_MIN = 0.1  # growth outside the controversy sections that counts as "a lot"


def bloats(struct: dict) -> bool:
    """PIPELINE.md section 5: the article grows a lot outside the controversy sections while
    their share falls and the controversy section moves down (or goes)."""
    then, now = struct["controversy_share_then"], struct["controversy_share_now"]
    return (
        then is not None
        and now is not None
        and now < then
        and struct["bloat_index"] >= BLOAT_MIN
        and controversy_drop(struct) > 0
    )


def extra_parts(pa: dict) -> dict:
    a, struct = pa["analytics"], pa["structure"]
    w = EXTRA_WEIGHTS
    parts = {
        "bloat": w["bloat"] * min(1, struct["bloat_index"]) if bloats(struct) else 0,
        "controversy_drop": w["controversy_drop"] * controversy_drop(struct),
        "reverted_90d": w["reverted_90d"] * min(1, a["reverted_90d"] / REVERTED_FULL),
        "bursts": w["bursts"] * min(1, len(a["burst_days"]) / BURSTS_FULL),
    }
    return {k: round(v, 1) for k, v in parts.items()}


def reason(pa: dict) -> str:
    a, struct = pa["analytics"], pa["structure"]
    bits = []
    move = rank_move(struct)
    then, now = struct["controversy_rank_then"], struct["controversy_rank_now"]
    if move > 0:
        bits.append(f"controversy section down {move} (#{then} -> #{now})")
    elif then is not None and now is None:
        bits.append(f"controversy section gone (was #{then})")
    if bloats(struct):
        bits.append(
            f"grew {struct['bloat_index']:+.0%} outside controversy sections, their share "
            f"{struct['controversy_share_then']:.0%} -> {struct['controversy_share_now']:.0%}"
        )
    if a["reverted_90d"]:
        bits.append(f"{a['reverted_90d']} reverted in {pa['days']}d")
    if a["burst_days"]:
        bits.append(f"{len(a['burst_days'])} burst day(s), latest {a['burst_days'][-1]}")
    if a["spa_count"]:
        bits.append(f"{a['spa_count']} single-purpose account(s)")
    if a["new_account_share"]:
        bits.append(f"{a['new_account_share']:.0%} of edits by new accounts")
    bits.append(f"top 3 editors made {a['top3_share']:.0%} of {a['edits_90d']} edits")
    return "; ".join(bits)


def rank(analyses: list[dict]) -> list[dict]:
    """Candidates by score (risk_score + extra points), highest first, ties by title."""
    rows = []
    for pa in analyses:
        extra = extra_parts(pa)
        struct = pa["structure"]
        rows.append(
            {
                "title": pa["title"],
                "pageid": pa["pageid"],
                "score": round(pa["risk_score"] + sum(extra.values()), 1),
                "risk_score": pa["risk_score"],
                "risk_parts": pa["risk_parts"],
                "extra_parts": extra,
                "reason": reason(pa),
                "edits_90d": pa["analytics"]["edits_90d"],
                "reverted_90d": pa["analytics"]["reverted_90d"],
                "bloat_index": struct["bloat_index"],
                "controversy_rank_then": struct["controversy_rank_then"],
                "controversy_rank_now": struct["controversy_rank_now"],
            }
        )
    rows.sort(key=lambda r: (-r["score"], r["title"]))
    return [{"rank": i, **r} for i, r in enumerate(rows, 1)]


def write(rows: list[dict], as_of: str, days: int, path: Path = PAGES_DIR / "candidates.json") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "as_of": as_of,
        "days": days,
        "computed_at": datetime.now(UTC).isoformat(),
        "weights": EXTRA_WEIGHTS,
        "candidates": rows,
    }
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False))
    return path
