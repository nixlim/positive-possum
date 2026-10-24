"""Repeat-run agreement (PIPELINE.md section 8): do the probe runs of a step still give the
outputs its first answered calls gave?

Repeat runs are rows with `kind == "probe"`, one `run_id` per run. A classify row's `extra`
carries `extract_call_id` (section 7.5): a probe classify row whose input is a probe-kind extract
row measures chained agreement; one whose input is a non-probe (baseline) extract row measures
identical-input agreement. A probe classify row without `extract_call_id`, or pointing to a row
not in the log given, is left out of both. Pure functions."""

from __future__ import annotations

from datetime import datetime

# Output fields compared by the repeat-run agreement; `review` is the model's own value.
AGREE_FIELDS = {
    "edit-extract": ("claims_added", "claims_removed", "negative_info", "tone_shift"),
    "edit-classify": ("label", "severity", "confidence", "review"),
}


def _output(step: str, row: dict) -> tuple:
    e = row.get("extra") or {}
    if step == "edit-classify":
        e = {**e, "review": e.get("review_model", e.get("review"))}
    return tuple(e.get(f) for f in AGREE_FIELDS[step])


def _agree(step: str, base: dict, run: dict, rows: list[dict]) -> dict | None:
    """Share of the run's answered revids whose output matches the baseline, or None when the run
    sent no such call."""
    if not rows:
        return None
    got: dict = {}
    for r in rows:
        if r.get("error") is None:
            got.setdefault(r["issue"], _output(step, r))
    same = [base[i] == out for i, out in got.items() if i in base]
    return {"value": sum(same) / len(same) if same else None, "n": len(same), **run}


def agreement(rows: list[dict]) -> dict[str, dict]:
    """Per step `{extract, classify, chained}`, each `{value, n, run_id, ts}` or None, over the
    step's latest probe run (errored calls included, so a fully errored run gives n=0). The
    baseline output of a revid is its first answered non-probe row; only revids answered in both
    count."""
    by_step: dict[str, list[dict]] = {}
    for r in sorted(rows, key=lambda r: (datetime.fromisoformat(r["ts"]), r.get("id") or 0)):
        by_step.setdefault(r["repo"], []).append(r)
    extract_kind = {r["id"]: r.get("kind") for r in by_step.get("edit-extract", [])}
    out = {}
    for step, calls in by_step.items():
        res: dict = {"extract": None, "classify": None, "chained": None}
        out[step] = res
        probes = [r for r in calls if r.get("kind") == "probe"]
        if step not in AGREE_FIELDS or not probes:
            continue
        base: dict = {}
        for r in calls:
            if r.get("kind") != "probe" and r.get("error") is None:
                base.setdefault(r["issue"], _output(step, r))
        # Calls are in time order, so the latest run is the one holding the last probe call.
        run = {"run_id": probes[-1]["run_id"], "ts": probes[-1]["ts"]}
        latest = [r for r in probes if r["run_id"] == run["run_id"]]
        if step == "edit-extract":
            res["extract"] = _agree(step, base, run, latest)
            continue
        source = {r["id"]: extract_kind.get((r.get("extra") or {}).get("extract_call_id")) for r in latest}
        res["classify"] = _agree(
            step, base, run, [r for r in latest if source[r["id"]] not in (None, "probe")]
        )
        res["chained"] = _agree(step, base, run, [r for r in latest if source[r["id"]] == "probe"])
    return out
