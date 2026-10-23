"""Scoring multi-label predictions against maintainer labels, and agreement
between two runs (the routing-switch signal)."""
from __future__ import annotations

from statistics import median

Labels = dict[int, list[str]]


def _family(label: str) -> str:
    for sep in (":", "/", "-", " "):
        if sep in label:
            return label.split(sep, 1)[0] + sep
    return "type"


def score(preds: Labels, truth: Labels) -> dict:
    """Exact set match rate and micro precision/recall/F1 over (issue, label) pairs.

    With no scored issue (e.g. every call errored) the rates are None, not 0: there is
    nothing to score, and a 0 would read as a drop in accuracy.
    """
    tp = fp = fn = exact = 0
    issues = [i for i in truth if i in preds]
    if not issues:
        return {"n": 0, "exact": None, "precision": None, "recall": None, "f1": None}
    for i in issues:
        p, t = set(preds[i]), set(truth[i])
        tp += len(p & t)
        fp += len(p - t)
        fn += len(t - p)
        exact += p == t
    n = len(issues)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"n": n, "exact": exact / n, "precision": precision,
            "recall": recall, "f1": f1}


def score_by_family(preds: Labels, truth: Labels) -> dict[str, dict]:
    families = {_family(l) for ls in truth.values() for l in ls} | {_family(l) for ls in preds.values() for l in ls}
    out = {}
    for fam in sorted(families):
        p = {i: [l for l in ls if _family(l) == fam] for i, ls in preds.items()}
        t = {i: [l for l in ls if _family(l) == fam] for i, ls in truth.items()}
        out[fam] = score(p, t)
    return out


def agreement(a: Labels, b: Labels) -> dict:
    """How often two runs give the same label set; mean Jaccard as a softer view.
    None when the runs share no issue."""
    issues = [i for i in a if i in b]
    if not issues:
        return {"n": 0, "exact": None, "jaccard": None}
    exact = sum(set(a[i]) == set(b[i]) for i in issues)
    jac = []
    for i in issues:
        sa, sb = set(a[i]), set(b[i])
        jac.append(len(sa & sb) / len(sa | sb) if sa | sb else 1.0)
    return {"n": len(issues), "exact": exact / len(issues), "jaccard": sum(jac) / len(jac)}


def latency_stats(ms: list[int]) -> dict:
    if not ms:
        return {"p50_ms": None, "p90_ms": None}
    s = sorted(ms)
    return {"p50_ms": int(median(s)), "p90_ms": int(s[min(len(s) - 1, int(len(s) * 0.9))])}
