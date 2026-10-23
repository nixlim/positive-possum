"""Eval and replay runs: send a split through Mubit, log every call, score."""

from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

from . import guidelines as gl
from .config import MODEL, RepoConfig, is_provider
from .github import load_dataset
from .llm import ROUTE, CallResult, classify, client
from .metrics import agreement, latency_stats, score, score_by_family
from .prompt import output_schema, system_prompt
from .store import Store


def _run_id(kind: str, repo: str) -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    return f"{kind}-{repo}-{stamp}-{uuid.uuid4().hex[:4]}"


def task_sha(cfg: RepoConfig, labels: list[dict]) -> str:
    """The Mubit task identity besides the model: system prompt + output schema."""
    ident = system_prompt(cfg.full_name, labels, cfg.kind) + json.dumps(
        output_schema(labels, cfg.kind), sort_keys=True
    )
    return hashlib.sha256(ident.encode()).hexdigest()[:12]


def served_by_provider(served: dict[str, int], model: str) -> bool:
    """True when every answered call in a run was served by the requested model itself
    (a date suffix allowed), i.e. Mubit forwarded to the provider and no adapter answered.
    '?' counts calls that got no reply at all; they say nothing about who serves."""
    answered = [m for m in served if m != "?"]
    return bool(answered) and all(is_provider(m, model) for m in answered)


def reference_run(
    store: Store, repo: str, model: str, task: str, guidelines_sha: str
) -> tuple[dict | None, str]:
    """The earliest scored eval run of the same task, model and guidelines that the provider
    served. Guidelines change without forking the Mubit task, so the first run after a
    correction may already be served by the adapter: such a run is never the reference.
    Returns (run, "") or (None, why there is none)."""
    same = [
        r
        for r in store.runs(repo)
        if r["kind"] == "eval"
        and r["n"] > 0
        and r.get("model") == model
        and r.get("task_sha") == task
        and r.get("guidelines_sha") == guidelines_sha
    ]
    for r in same:
        if served_by_provider(r.get("served") or {}, model):
            return r, ""
    if same:
        return None, (
            f"no scored eval run with the same task, guidelines and model was served by {model} "
            f"alone; the earliest, {same[0]['run_id']}, was served by {same[0].get('served')}"
        )
    return None, "no scored eval run with the same task, guidelines and model"


def run(
    cfg: RepoConfig,
    kind: str,
    store: Store,
    limit: int | None = None,
    workers: int = 4,
    model: str = MODEL,
    verbose: bool = True,
) -> dict:
    labels, rows = load_dataset(cfg)
    split = "eval" if kind == "eval" else "train"
    issues = [r for r in rows if r["split"] == split]
    if limit:
        issues = issues[:limit]
    if not issues:
        raise SystemExit(f"no {split} issues for {cfg.key}; run `triage pull` first")
    guide = gl.load(cfg.key, eval_issues={r["number"] for r in rows if r["split"] == "eval"})
    c = client()
    run_id = _run_id(kind, cfg.key)

    def one(issue: dict) -> tuple[dict, CallResult]:
        return issue, classify(c, cfg.full_name, labels, issue, guide, model=model, kind=cfg.kind)

    results: list[tuple[dict, CallResult]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for issue, res in pool.map(one, issues):
            store.log_call(run_id, kind, cfg.key, issue["number"], res, truth=issue["labels"])
            results.append((issue, res))
            if verbose:
                mark = "ERR" if res.error else "ok "
                print(
                    f"  {mark} #{issue['number']:<7} {res.latency_ms:>6}ms  {res.model_served or '-':<28} "
                    f"pred={res.labels} truth={issue['labels']}" + (f"  {res.error}" if res.error else "")
                )

    good = [(i, r) for i, r in results if not r.error]
    preds = {i["number"]: r.labels for i, r in good}
    truth = {i["number"]: i["labels"] for i, _ in good}
    metrics = {
        **score(preds, truth),
        "errors": len(results) - len(good),  # logged above, never scored
        "by_family": score_by_family(preds, truth),
        **latency_stats([r.latency_ms for _, r in good]),
        "served": store.served_models(run_id),
        "unknown_labels": sum(len(r.unknown_labels) for _, r in good),
        "model": model,
        "guidelines_sha": hashlib.sha256(guide.encode()).hexdigest()[:12],
        "task_sha": task_sha(cfg, labels),
    }
    if kind == "eval":
        ref, why = reference_run(store, cfg.key, model, metrics["task_sha"], metrics["guidelines_sha"])
        if ref:
            metrics["reference_run"] = ref["run_id"]
            metrics["reference_served"] = ref.get("served") or {}
            metrics["agreement_vs_reference"] = agreement(preds, store.predictions(ref["run_id"]))
        elif any(r["kind"] == "eval" for r in store.runs(cfg.key)):
            metrics["reference_mismatch"] = why
    store.log_run(run_id, kind, ROUTE, cfg.key, len(results), metrics["errors"], metrics)
    metrics["run_id"] = run_id
    return metrics
