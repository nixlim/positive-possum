"""Timeline of eval runs per repo: the chart for the demo, as text."""

from __future__ import annotations

from .store import Store


def _pct(x: float | None) -> str:
    return "   -" if x is None else f"{100 * x:4.0f}"


def render(store: Store, repo: str | None = None) -> str:
    runs = [r for r in store.runs(repo) if r["kind"] == "eval"]
    if not runs:
        return "no eval runs yet"
    lines = [
        f"{'time (UTC)':<20} {'repo':<8} {'route':<7} {'n':>3} {'err':>3} {'exact%':>6} {'F1%':>5} "
        f"{'agree%':>6} {'p50ms':>6} {'p90ms':>6}  served by",
    ]
    for r in runs:
        agree = (r.get("agreement_vs_reference") or {}).get("exact")
        served = ", ".join(f"{m}×{c}" for m, c in (r.get("served") or {}).items())
        lines.append(
            f"{r['ts'][:19]:<20} {r['repo']:<8} {r['route']:<7} {r['n']:>3} {r['errors']:>3} {_pct(r['exact']):>6} "
            f"{_pct(r['f1']):>5} {_pct(agree):>6} {str(r.get('p50_ms') or '-'):>6} "
            f"{str(r.get('p90_ms') or '-'):>6}  {served}"
        )
    return "\n".join(lines)


def render_families(store: Store, run_id: str) -> str:
    runs = {r["run_id"]: r for r in store.runs()}
    r = runs.get(run_id)
    if not r:
        return f"no run {run_id}"
    lines = [f"{'family':<12} {'n':>3} {'exact%':>6} {'F1%':>5}"]
    for fam, m in (r.get("by_family") or {}).items():
        lines.append(f"{fam:<12} {m['n']:>3} {_pct(m['exact']):>6} {_pct(m['f1']):>5}")
    return "\n".join(lines)
