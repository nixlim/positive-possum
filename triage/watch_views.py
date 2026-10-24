"""Pure builders for the watchlist API (PIPELINE.md section 9): Edit, PageAnalysis, watchlist rows,
Mubit status per step, the cost estimate and live events, from snapshots and logged call rows.

`extracted` and `classified` are `Store.answered` of the extract and classify steps with probes
left out, so an Edit carries the same classification `pipeline.logged_edits` rebuilds."""

from __future__ import annotations

from collections.abc import Collection
from statistics import fmean

from . import edit_state as es
from . import page_analytics as pa
from . import provenance, steps
from .classification import classification, extract_of
from .config import PRICE_USD_PER_MTOK
from .wiki import editor_kind

STEPS = (steps.EXTRACT, steps.CLASSIFY, steps.BRIEF)
STEP_KEYS = (
    "requests",
    "answered",
    "errors",
    "served_by",
    "provider_share",
    "latency_p50_ms",
    "tokens_in",
    "tokens_out",
    "shift",
    "agreement",
)
REV_FIELDS = (
    "revid",
    "parentid",
    "timestamp",
    "editor_kind",
    "comment",
    "size_delta",
    "reverted",
    "reverting",
)
SPARK_DAYS = 30
COST_ASSUMPTIONS = [
    "provider list price for the asked model (config.PRICE_USD_PER_MTOK), not what Mubit charges",
    "calls per edit and tokens per call are the means over answered edit-extract and edit-classify "
    "calls in the log, probes and retries included",
    "an edit is a revision with an answered extract; edits the skip rule settles need no classify call",
    "the page brief is not included: it is one call per article, not per edit",
]


def diff_url(revid: int, parentid: int | None) -> str:
    base = f"https://en.wikipedia.org/w/index.php?diff={revid}"
    return base if parentid is None else f"{base}&oldid={parentid}"


def edit(rev: dict, title: str | None, ext: dict | None, cls: dict | None) -> dict:
    """An Edit from its revision fields and its logged extract and classify rows; the log fields
    are null until the edit is extracted, and fields the revision does not carry are null."""
    out = {k: rev.get(k) for k in REV_FIELDS} | {
        "title": title,
        "diff_url": diff_url(rev["revid"], rev.get("parentid")),
    }
    if ext is None:
        return out | {
            "measured": None,
            "extract": None,
            "classification": None,
            "served": {"extract": None, "classify": None},
        }
    return out | {
        "measured": ext["extra"]["measured"],
        "extract": extract_of(ext["extra"]),
        "classification": classification(ext["extra"], cls["extra"] if cls else None),
        "served": {"extract": ext["model_served"], "classify": cls["model_served"] if cls else None},
    }


def page_edits(snap: dict, extracted: dict, classified: dict) -> dict[int, dict]:
    """Edit per snapshot revision that has an extract row: the `edits` page_analytics takes."""
    return {
        r["revid"]: edit(r, snap["title"], extracted[r["revid"]], classified.get(r["revid"]))
        for r in snap["revisions"]
        if r["revid"] in extracted
    }


def page_analysis(
    snap: dict, log: es.Log, pending: Collection[int] = (), stopped: str | None = None, computed_at=None
) -> dict:
    """PageAnalysis: the analytics, the window's flagged edits newest first, and where the
    classification of the window's edits stands (edit_state); `risk_partial` is true exactly
    when one of them is pending, errored or open, whatever page_analytics says."""
    edits = page_edits(snap, log.extracted, log.classified)
    flags = [
        edits[r["revid"]]
        for r in reversed(snap["revisions"])  # snapshots keep revisions oldest first
        if r["revid"] in edits and pa.is_flagged(edits[r["revid"]]["classification"])
    ]
    part = es.partition(log, [r["revid"] for r in pa.recent(snap)], pending)
    counts = {
        "classify_pending": len(part[es.PENDING]),
        "classify_errored": len(part[es.ERRORED]),
        "classify_open": len(part[es.OPEN]),
    }
    return pa.analyse(snap, edits, computed_at) | {
        "flags": flags,
        **counts,
        "classify_stopped": stopped,
        "risk_partial": sum(counts.values()) > 0,
    }


def watchlist_row(snap: dict, analysis: dict) -> dict:
    a = analysis["analytics"]
    return {
        "title": analysis["title"],
        "pageid": analysis["pageid"],
        "risk_score": analysis["risk_score"],
        "risk_partial": analysis["risk_partial"],
        "flagged_30d": a["flagged_30d"],
        "edits_30d": a["edits_30d"],
        "reverted_30d": a["reverted_30d"],
        "last_edit_at": max((r["timestamp"] for r in snap["revisions"]), default=None),
        "spark": [d["count"] for d in a["edits_per_day"][-SPARK_DAYS:]],
    }


def watchlist_rows(pages: list[tuple[dict, dict]]) -> list[dict]:
    """One row per ingested watchlist page (its snapshot and PageAnalysis), riskiest first."""
    rows = [watchlist_row(snap, analysis) for snap, analysis in pages]
    return sorted(rows, key=lambda r: (-r["risk_score"], r["title"]))


def _no_calls() -> dict:
    return {
        **provenance.totals([]),
        "shift": {"hard": [], "soft": []},
        "agreement": {"extract": None, "classify": None, "chained": None},
    }


def status(rows: list[dict], console: dict, mubit: dict, wikipedia: dict, now=None) -> dict:
    """Per step, provenance's report cut to the contract's keys (a step with no calls is all
    zero) with the step's hand-copied console fields, plus both budgets."""
    rep = provenance.report([r for r in rows if r["repo"] in STEPS], now=now)
    out = {
        step: {k: (rep.get(step) or _no_calls())[k] for k in STEP_KEYS} | {"console": console.get(step) or {}}
        for step in STEPS
    }
    return out | {"mubit": mubit, "wikipedia": wikipedia}


def cost(rows: list[dict], price: dict = PRICE_USD_PER_MTOK) -> dict:
    """What 1,000 edits cost at the provider's list price, from the logged token counts."""
    ok = [r for r in rows if r["repo"] in (steps.EXTRACT, steps.CLASSIFY) and not r["error"]]
    edits = {r["issue"] for r in ok if r["repo"] == steps.EXTRACT}
    out = {
        "provider_per_1k_edits": None,
        "calls_per_edit": None,
        "tokens_per_call": None,
        "price": price,
        "assumptions": COST_ASSUMPTIONS,
    }
    if not edits:
        return out
    per_edit = len(ok) / len(edits)
    tokens = {
        "input": fmean(r["prompt_tokens"] or 0 for r in ok),
        "output": fmean(r["completion_tokens"] or 0 for r in ok),
    }
    per_call = (tokens["input"] * price["input"] + tokens["output"] * price["output"]) / 1e6
    return out | {
        "provider_per_1k_edits": round(1000 * per_edit * per_call, 2),
        "calls_per_edit": round(per_edit, 3),
        "tokens_per_call": {k: round(v) for k, v in tokens.items()},
    }


def live_event(rc: dict, result: dict | None, stopped: str | None = None) -> dict:
    """A live feed entry; label, severity and review are null when the edit was not classified,
    and `stopped` says which budget ("budget" for Mubit, "wikipedia") held it back, if one did."""
    cls = (result or {}).get("classification") or {}
    return {
        "stopped": stopped,
        "revid": rc["revid"],
        "title": rc["title"],
        "user_kind": editor_kind(rc),
        "label": cls.get("label"),
        "severity": cls.get("severity"),
        "review": cls.get("review"),
        "ts": rc["timestamp"],
        "served": (result or {}).get("served") or {"extract": None, "classify": None},
    }
