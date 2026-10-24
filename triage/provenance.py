"""Did anything about who answers a step change? Mubit offers no way to ask which model served
a call, so this reads side effects from our own log (served model, headers, reply shape,
latency, tokens) and compares each step's recent calls with its first ones. Pure functions
except `read_calls`."""

from __future__ import annotations

import json
import re
import statistics
from datetime import UTC, datetime, timedelta

from .config import is_provider
from .store import Store

# A change in any of these is a hard signal; latency and output tokens only move softly.
CATEGORICAL = ("served", "provider_headers", "id_prefix", "usage_keys")
SOFT = ("latency_ms", "output_tokens")
ORIGIN = re.compile(r"x-originResponse;\s*dur=([\d.]+)", re.I)
ID_PREFIX = re.compile(r"[A-Za-z]+[_-]")
COLS = (
    "id",
    "ts",
    "repo",
    "kind",
    "model_asked",
    "model_served",
    "latency_ms",
    "completion_tokens",
    "error",
    "headers",
    "raw",
)


def read_calls(store: Store | None = None, step: str | None = None) -> list[dict]:
    """Every logged call (errors included), oldest first, with headers and raw reply decoded."""
    q = f"SELECT {', '.join(COLS)} FROM calls"
    args: tuple = ()
    if step:
        q += " WHERE repo=?"
        args = (step,)
    rows = []
    for values in (store or Store()).db.execute(q + " ORDER BY ts, id", args):
        r = dict(zip(COLS, values, strict=True))
        r["headers"] = json.loads(r["headers"]) if r["headers"] else {}
        r["raw"] = json.loads(r["raw"]) if r["raw"] else None
        rows.append(r)
    return rows


def _provider_headers(headers: dict) -> tuple[str, ...]:
    """Which of Anthropic's own reply headers came through Mubit's proxy."""
    marks = {
        "anthropic-ratelimit-*" if name.startswith("anthropic-ratelimit-") else name
        for name in headers
        if name == "anthropic-organization-id" or name.startswith("anthropic-ratelimit-")
    }
    if str(headers.get("request-id") or "").startswith("req_"):
        marks.add("request-id: req_")
    return tuple(sorted(marks))


def fingerprint(row: dict) -> dict:
    headers = {k.lower(): v for k, v in (row.get("headers") or {}).items()}
    raw = row.get("raw") or {}
    usage = raw.get("usage")
    prefix = ID_PREFIX.match(str(raw.get("id") or ""))
    origin = ORIGIN.search(str(headers.get("server-timing") or ""))
    served = row.get("model_served")
    return {
        "served": served,
        "provider": is_provider(served, row.get("model_asked") or ""),
        "provider_headers": _provider_headers(headers),
        "id_prefix": prefix.group(0) if prefix else None,
        "usage_keys": tuple(sorted(usage)) if isinstance(usage, dict) else (),
        "origin_ms": float(origin.group(1)) if origin else None,
        "latency_ms": row.get("latency_ms"),
        "output_tokens": row.get("completion_tokens"),
        "ok": not row.get("error"),
    }


def _key(fp: dict) -> tuple:
    return tuple(fp[f] for f in CATEGORICAL)


def _ts(row: dict) -> datetime:
    return datetime.fromisoformat(row["ts"])


def _by_step(rows: list[dict]) -> dict[str, list[tuple[dict, dict]]]:
    steps: dict[str, list[tuple[dict, dict]]] = {}
    for r in sorted(rows, key=lambda r: (_ts(r), r.get("id") or 0)):
        steps.setdefault(r["repo"], []).append((r, fingerprint(r)))
    return steps


def _groups(calls: list[tuple[dict, dict]]) -> dict[tuple, dict]:
    """Distinct categorical fingerprints with their count and first time seen."""
    groups: dict[tuple, dict] = {}
    for r, fp in calls:
        g = groups.setdefault(_key(fp), {**{f: fp[f] for f in CATEGORICAL}, "count": 0, "first_ts": r["ts"]})
        g["count"] += 1
    return groups


def _hard(base: list[tuple[dict, dict]], recent: list[tuple[dict, dict]]) -> list[dict]:
    known = _groups(base)
    seen = {f: {k[i] for k in known} for i, f in enumerate(CATEGORICAL)}
    return [
        {**g, "changed": [f for f in CATEGORICAL if g[f] not in seen[f]]}
        for k, g in _groups(recent).items()
        if k not in known
    ]


def _soft(base: list[tuple[dict, dict]], recent: list[tuple[dict, dict]]) -> list[dict]:
    signals = []
    for metric in SOFT:
        b = [fp[metric] for _, fp in base if fp[metric] is not None]
        r = [fp[metric] for _, fp in recent if fp[metric] is not None]
        if len(b) < 2 or not r:
            continue
        q1, _, q3 = statistics.quantiles(b, n=4)
        med = statistics.median(r)
        if not q1 <= med <= q3:
            signals.append(
                {
                    "metric": metric,
                    "direction": "down" if med < q1 else "up",
                    "recent_median": med,
                    "baseline_median": statistics.median(b),
                    "baseline_iqr": [q1, q3],
                    "n_recent": len(r),
                }
            )
    return signals


def shift(
    rows: list[dict], baseline_n: int = 200, recent_hours: float = 24, now: datetime | None = None
) -> dict:
    """Per step: the first `baseline_n` answered calls against the answered calls after them
    in the last `recent_hours`. Errored calls are counted but never compared."""
    since = (now or datetime.now(UTC)) - timedelta(hours=recent_hours)
    out = {}
    for step, calls in _by_step(rows).items():
        ok = [c for c in calls if c[1]["ok"]]
        base = ok[:baseline_n]
        recent = [c for c in ok[baseline_n:] if _ts(c[0]) >= since]
        out[step] = {
            "baseline": {
                "n": len(base),
                "from": base[0][0]["ts"] if base else None,
                "to": base[-1][0]["ts"] if base else None,
                "fingerprints": list(_groups(base).values()),
            },
            "recent": {"n": len(recent), "since": since.isoformat(timespec="seconds")},
            "errors_excluded": len(calls) - len(ok),
            "shift": {"hard": _hard(base, recent), "soft": _soft(base, recent)},
        }
    return out


def _median(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def _hour(hour: str, fps: list[dict]) -> dict:
    ok = [fp for fp in fps if fp["ok"]]
    return {
        "hour": hour,
        "calls": len(fps),
        "errors": len(fps) - len(ok),
        "provider_share": sum(fp["provider"] for fp in ok) / len(ok) if ok else None,
        "p50_latency_ms": _median([fp["latency_ms"] for fp in ok]),
        "p50_output_tokens": _median([fp["output_tokens"] for fp in ok]),
        "fingerprints": len({_key(fp) for fp in ok}),
    }


def hourly(rows: list[dict]) -> dict[str, list[dict]]:
    """Per step and UTC hour; the rates and medians are over answered calls only."""
    out = {}
    for step, calls in _by_step(rows).items():
        hours: dict[str, list[dict]] = {}
        for r, fp in calls:
            hours.setdefault(_ts(r).astimezone(UTC).strftime("%Y-%m-%dT%H:00Z"), []).append(fp)
        out[step] = [_hour(h, fps) for h, fps in sorted(hours.items())]
    return out


def report(rows: list[dict], baseline_n: int = 200, hours: float = 24, now: datetime | None = None) -> dict:
    """`shift` per step, with that step's hourly rows for the same recent window."""
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(hours=hours)).astimezone(UTC).strftime("%Y-%m-%dT%H:00Z")
    out = shift(rows, baseline_n, hours, now)
    for step, hrs in hourly(rows).items():
        out[step]["hourly"] = [h for h in hrs if h["hour"] >= cutoff]
    return out


def _fp_text(g: dict) -> str:
    headers = ", ".join(g["provider_headers"]) or "none"
    return f"served={g['served']} id={g['id_prefix']} provider_headers=[{headers}] usage={','.join(g['usage_keys'])}"


def _num(x: float | None) -> str:
    return "-" if x is None else f"{x:.0f}"


def render(rep: dict) -> str:
    lines = []
    for step, s in rep.items():
        b, sig = s["baseline"], s["shift"]
        lines.append(
            f"== {step}: baseline {b['n']} answered calls ({b['from']} .. {b['to']}); "
            f"recent {s['recent']['n']} since {s['recent']['since']}; {s['errors_excluded']} errored excluded"
        )
        lines += [f"  baseline {g['count']:>5}  {_fp_text(g)}" for g in b["fingerprints"]]
        if not sig["hard"] and not sig["soft"]:
            lines.append("  no signal")
        for g in sig["hard"]:
            changed = ",".join(g["changed"]) or "combination"
            lines.append(f"  HARD {g['count']:>5} from {g['first_ts']} changed={changed}  {_fp_text(g)}")
        for x in sig["soft"]:
            q1, q3 = x["baseline_iqr"]
            lines.append(
                f"  soft {x['metric']} {x['direction']}: recent median {_num(x['recent_median'])} vs baseline "
                f"{_num(x['baseline_median'])} (IQR {_num(q1)}-{_num(q3)}), n={x['n_recent']}"
            )
        lines.append(
            f"  {'hour (UTC)':<17} {'calls':>5} {'err':>4} {'prov%':>5} {'p50ms':>6} {'p50out':>6} {'fps':>3}"
        )
        for h in s.get("hourly", []):
            share = "-" if h["provider_share"] is None else f"{100 * h['provider_share']:.0f}"
            lines.append(
                f"  {h['hour']:<17} {h['calls']:>5} {h['errors']:>4} {share:>5} {_num(h['p50_latency_ms']):>6} "
                f"{_num(h['p50_output_tokens']):>6} {h['fingerprints']:>3}"
            )
    return "\n".join(lines) or "no calls logged"
