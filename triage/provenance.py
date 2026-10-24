"""Did anything about who answers a step change? Mubit offers no way to ask which model served
a call, so this reads side effects from our own log (served model, headers, reply shape,
latency, tokens) and compares each step's recent answered calls with its first provider-served
ones (PIPELINE.md section 8). Errored calls are counted in two groups and never scored.
Repeat-run agreement lives in agreement.py. Pure functions except `read_calls`."""

from __future__ import annotations

import json
import re
import statistics
from collections import Counter
from datetime import UTC, datetime, timedelta

from .agreement import agreement
from .config import is_provider
from .store import Store

# Distinct values of these make a distinct fingerprint group.
CATEGORICAL = ("served", "provider_headers", "id_prefix", "usage_keys", "provider_misses")
# Against the baseline a new value of any of these is a hard signal; latency and output tokens
# only move softly.
BASELINE_HARD = ("id_prefix", "usage_keys", "ratelimit")
SOFT = ("latency_ms", "output_tokens")
PROVIDER_TEST = "served is the asked model, anthropic-organization-id, request-id req_"
ORIGIN = re.compile(r"x-originResponse;\s*dur=([\d.]+)", re.I)
ID_PREFIX = re.compile(r"[A-Za-z]+[_-]")
COLS = (
    "id",
    "ts",
    "run_id",
    "kind",
    "repo",
    "issue",
    "model_asked",
    "model_served",
    "latency_ms",
    "prompt_tokens",
    "completion_tokens",
    "error",
    "headers",
    "raw",
    "extra",
)


def read_calls(store: Store | None = None, step: str | None = None) -> list[dict]:
    """Every logged call (errors included), oldest first, with headers, raw and extra decoded."""
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
        r["extra"] = json.loads(r["extra"]) if r["extra"] else {}
        rows.append(r)
    return rows


def error_group(error: str | None) -> str | None:
    """`unparseable` for an HTTP 200 reply that failed to parse, `gateway` for any other error
    (`HTTP {status}: …` or `{Exception}: …` from `llm.classify`), None when answered."""
    if error is None:
        return None
    return "unparseable" if error.startswith("unparseable reply:") else "gateway"


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
    """`provider` is the provider test: served is the asked model and the reply carries
    Anthropic's organization id and a `req_` request id. Rate-limit headers are left out of the
    test but stay in the fingerprint, where a change against the baseline is hard (section 8)."""
    headers = {k.lower(): v for k, v in (row.get("headers") or {}).items()}
    raw = row.get("raw") or {}
    usage = raw.get("usage")
    prefix = ID_PREFIX.match(str(raw.get("id") or ""))
    origin = ORIGIN.search(str(headers.get("server-timing") or ""))
    served = row.get("model_served")
    marks = _provider_headers(headers)
    checks = (
        ("served", is_provider(served, row.get("model_asked") or "")),
        ("anthropic-organization-id", "anthropic-organization-id" in marks),
        ("request-id: req_", "request-id: req_" in marks),
    )
    misses = tuple(name for name, passed in checks if not passed)
    return {
        "served": served,
        "provider": not misses,
        "provider_misses": misses,
        "provider_headers": marks,
        "ratelimit": "anthropic-ratelimit-*" in marks,
        "id_prefix": prefix.group(0) if prefix else None,
        "usage_keys": tuple(sorted(usage)) if isinstance(usage, dict) else (),
        "origin_ms": float(origin.group(1)) if origin else None,
        "latency_ms": row.get("latency_ms"),
        "input_tokens": row.get("prompt_tokens"),
        "output_tokens": row.get("completion_tokens"),
        "ok": row.get("error") is None,
        "error": error_group(row.get("error")),
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
        fields = {f: fp[f] for f in (*CATEGORICAL, "provider", "ratelimit")}
        g = groups.setdefault(_key(fp), {**fields, "count": 0, "first_ts": r["ts"]})
        g["count"] += 1
    return groups


def _hard(base: list[tuple[dict, dict]], recent: list[tuple[dict, dict]]) -> list[dict]:
    """A recent group failing the provider test is hard on its own; against a baseline, so is a
    new id prefix, usage key set or rate-limit header presence."""
    seen = {f: {fp[f] for _, fp in base} for f in BASELINE_HARD}
    out = []
    for g in _groups(recent).values():
        changed = list(g["provider_misses"]) + [f for f in BASELINE_HARD if base and g[f] not in seen[f]]
        if changed:
            out.append({**g, "changed": changed})
    return out


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


def _median(xs: list) -> float | None:
    xs = [x for x in xs if x is not None]
    return statistics.median(xs) if xs else None


def totals(fps: list[dict]) -> dict:
    """Counts over every call; everything after `errors` is over answered calls only. The
    unparseable rate is per HTTP 200 reply (answered + unparseable)."""
    ok = [fp for fp in fps if fp["ok"]]
    errors = Counter(fp["error"] for fp in fps if not fp["ok"])
    replies = len(ok) + errors["unparseable"]
    provider = sum(fp["provider"] for fp in ok)
    return {
        "requests": len(fps),
        "answered": len(ok),
        "errors": {"gateway": errors["gateway"], "unparseable": errors["unparseable"]},
        "unparseable_rate": errors["unparseable"] / replies if replies else None,
        "served_by": dict(Counter(fp["served"] for fp in ok)),
        "provider_served": provider,
        "provider_share": provider / len(ok) if ok else None,
        "latency_p50_ms": _median([fp["latency_ms"] for fp in ok]),
        "output_tokens_p50": _median([fp["output_tokens"] for fp in ok]),
        "tokens_in": sum(fp["input_tokens"] or 0 for fp in ok),
        "tokens_out": sum(fp["output_tokens"] or 0 for fp in ok),
        "fingerprints": len({_key(fp) for fp in ok}),
    }


def shift(
    rows: list[dict], baseline_n: int = 200, recent_hours: float = 24, now: datetime | None = None
) -> dict:
    """Per step: the step's totals, and the first `baseline_n` answered provider-served calls
    against the other answered calls of the last `recent_hours`."""
    since = (now or datetime.now(UTC)) - timedelta(hours=recent_hours)
    out = {}
    for step, calls in _by_step(rows).items():
        ok = [c for c in calls if c[1]["ok"]]
        base = [c for c in ok if c[1]["provider"]][:baseline_n]
        in_base = {id(c) for c in base}
        recent = [c for c in ok if id(c) not in in_base and _ts(c[0]) >= since]
        out[step] = {
            **totals([fp for _, fp in calls]),
            "baseline": {
                "n": len(base),
                "from": base[0][0]["ts"] if base else None,
                "to": base[-1][0]["ts"] if base else None,
                "fingerprints": list(_groups(base).values()),
            },
            "recent": {"n": len(recent), "since": since.isoformat(timespec="seconds")},
            "shift": {"hard": _hard(base, recent), "soft": _soft(base, recent)},
        }
    return out


def hourly(rows: list[dict]) -> dict[str, list[dict]]:
    """Per step and UTC hour, `totals` of that hour's calls."""
    out = {}
    for step, calls in _by_step(rows).items():
        hours: dict[str, list[dict]] = {}
        for r, fp in calls:
            hours.setdefault(_ts(r).astimezone(UTC).strftime("%Y-%m-%dT%H:00Z"), []).append(fp)
        out[step] = [{"hour": h, **totals(fps)} for h, fps in sorted(hours.items())]
    return out


def report(rows: list[dict], baseline_n: int = 200, hours: float = 24, now: datetime | None = None) -> dict:
    """`shift` per step, with its agreement and its hourly rows for the same recent window."""
    now = now or datetime.now(UTC)
    cutoff = (now - timedelta(hours=hours)).astimezone(UTC).strftime("%Y-%m-%dT%H:00Z")
    out = shift(rows, baseline_n, hours, now)
    agree = agreement(rows)
    for step, hrs in hourly(rows).items():
        out[step]["agreement"] = agree[step]
        out[step]["hourly"] = [h for h in hrs if h["hour"] >= cutoff]
    return out


def _fp_text(g: dict) -> str:
    headers = ", ".join(g["provider_headers"]) or "none"
    test = "pass" if g["provider"] else "FAIL"
    return (
        f"provider={test} served={g['served']} id={g['id_prefix']} provider_headers=[{headers}] "
        f"usage={','.join(g['usage_keys'])}"
    )


def _num(x: float | None) -> str:
    return "-" if x is None else f"{x:.0f}"


def _pct(x: float | None, digits: int = 0) -> str:
    return "-" if x is None else f"{100 * x:.{digits}f}%"


def _agree_text(a: dict | None) -> str:
    return "-" if a is None else f"{_pct(a['value'])} (n={a['n']}, run {a['run_id']} at {a['ts']})"


def _signals(sig: dict) -> list[str]:
    lines = [] if sig["hard"] or sig["soft"] else ["  no signal"]
    for g in sig["hard"]:
        changed = ",".join(g["changed"])
        lines.append(f"  HARD {g['count']:>5} from {g['first_ts']} changed={changed}  {_fp_text(g)}")
    for x in sig["soft"]:
        q1, q3 = x["baseline_iqr"]
        lines.append(
            f"  soft {x['metric']} {x['direction']}: recent median {_num(x['recent_median'])} vs baseline "
            f"{_num(x['baseline_median'])} (IQR {_num(q1)}-{_num(q3)}), n={x['n_recent']}"
        )
    return lines


def _hourly_lines(hrs: list[dict]) -> list[str]:
    lines = [
        f"  {'hour (UTC)':<17} {'req':>5} {'ok':>5} {'gw':>4} {'unp':>4} {'prov%':>5} "
        f"{'p50ms':>6} {'p50out':>6} {'fps':>3}"
    ]
    for h in hrs:
        e = h["errors"]
        lines.append(
            f"  {h['hour']:<17} {h['requests']:>5} {h['answered']:>5} {e['gateway']:>4} "
            f"{e['unparseable']:>4} {_pct(h['provider_share']):>5} {_num(h['latency_p50_ms']):>6} "
            f"{_num(h['output_tokens_p50']):>6} {h['fingerprints']:>3}"
        )
    return lines


def render(rep: dict) -> str:
    lines = []
    for step, s in rep.items():
        b, e = s["baseline"], s["errors"]
        lines += [
            f"== {step}: {s['requests']} requests, {s['answered']} answered; errors (never scored): "
            f"gateway {e['gateway']}, unparseable {e['unparseable']} "
            f"(unparseable rate {_pct(s['unparseable_rate'], 1)})",
            f"  provider test ({PROVIDER_TEST}): {s['provider_served']}/{s['answered']} answered pass",
            f"  baseline {b['n']} provider-served calls ({b['from']} .. {b['to']}); "
            f"recent {s['recent']['n']} answered since {s['recent']['since']}",
        ]
        lines += [f"  baseline {g['count']:>5}  {_fp_text(g)}" for g in b["fingerprints"]]
        lines += _signals(s["shift"])
        if "agreement" in s:
            a = s["agreement"]
            lines.append(
                f"  agreement with baseline output: extract {_agree_text(a['extract'])}, "
                f"classify {_agree_text(a['classify'])}, chained {_agree_text(a['chained'])}"
            )
        lines += _hourly_lines(s.get("hourly", []))
    return "\n".join(lines) or "no calls logged"
