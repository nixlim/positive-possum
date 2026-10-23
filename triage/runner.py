"""Eval and replay runs: send a split through a route, log every call, score."""
from __future__ import annotations

import hashlib
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from . import guidelines as gl
from .config import MODEL, RepoConfig
from .github import load_dataset
from .llm import CallResult, classify, client_for
from .metrics import agreement, latency_stats, score, score_by_family
from .prompt import response_format, system_prompt
from .store import Store


def _run_id(kind: str, repo: str, route: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    return f"{kind}-{repo}-{route}-{stamp}-{uuid.uuid4().hex[:4]}"


def run(cfg: RepoConfig, route: str, kind: str, store: Store, limit: int | None = None,
        workers: int = 4, model: str = MODEL, verbose: bool = True) -> dict:
    labels, rows = load_dataset(cfg)
    split = "eval" if kind == "eval" else "train"
    issues = [r for r in rows if r["split"] == split]
    if limit:
        issues = issues[:limit]
    if not issues:
        raise SystemExit(f"no {split} issues for {cfg.key}; run `triage pull` first")
    guide = gl.load(cfg.key, eval_issues={r["number"] for r in rows if r["split"] == "eval"})
    client = client_for(route)
    run_id = _run_id(kind, cfg.key, route)

    def one(issue: dict) -> tuple[dict, CallResult]:
        return issue, classify(client, route, cfg.full_name, labels, issue, guide, model=model)

    results: list[tuple[dict, CallResult]] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for issue, res in pool.map(one, issues):
            store.log_call(run_id, kind, cfg.key, issue["number"], res, truth=issue["labels"])
            results.append((issue, res))
            if verbose:
                mark = "ERR" if res.error else "ok "
                print(f"  {mark} #{issue['number']:<7} {res.latency_ms:>6}ms  {res.model_served or '-':<28} "
                      f"pred={res.labels} truth={issue['labels']}" + (f"  {res.error}" if res.error else ""))

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
        # The Mubit task identity besides the model: system prompt + output schema.
        "task_sha": hashlib.sha256((system_prompt(cfg.full_name, labels)
                                    + json.dumps(response_format(labels), sort_keys=True)).encode()
                                   ).hexdigest()[:12],
    }
    if route == "mubit" and kind == "eval":
        # Only compare with a direct run that saw the same task, guidelines and model, so a change
        # in agreement can come from Mubit routing alone, and that scored at least one issue.
        same = [r["run_id"] for r in store.runs(cfg.key)
                if r["kind"] == "eval" and r["route"] == "direct" and r["n"] > 0
                and r.get("guidelines_sha") == metrics["guidelines_sha"] and r.get("model") == model
                and r.get("task_sha") == metrics["task_sha"]]
        if same:
            metrics["agreement_vs_direct"] = agreement(preds, store.predictions(same[-1]))
            metrics["baseline_run"] = same[-1]
        elif store.latest_run(cfg.key, "direct", "eval"):
            metrics["baseline_mismatch"] = "no scored direct eval run with the same task, guidelines and model"
    store.log_run(run_id, kind, route, cfg.key, len(results), metrics["errors"], metrics)
    metrics["run_id"] = run_id
    return metrics
