"""`triage` command line."""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import guidelines as gl
from .config import DATA_DIR, MODEL, REPOS, repo
from .github import build_dataset, load_dataset
from .llm import classify, client
from .report import render, render_families
from .runner import run
from .store import Store


def cmd_pull(a):
    path = build_dataset(repo(a.repo), a.n)
    labels, rows = load_dataset(repo(a.repo))
    n_eval = sum(r["split"] == "eval" for r in rows)
    n_corr = sum(r["corrected"] for r in rows)
    print(f"wrote {path}: {len(rows)} issues ({n_eval} eval, {len(rows) - n_eval} train), "
          f"{len(labels)} target labels, {n_corr} issues with maintainer corrections after first pass")


def cmd_eval(a):
    m = run(repo(a.repo), "eval", Store(), limit=a.limit, workers=a.workers, model=a.model)
    print(json.dumps({k: v for k, v in m.items() if k != "by_family"}, indent=1))


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def cmd_replay(a):
    m = run(repo(a.repo), "replay", Store(), limit=a.limit, workers=a.workers, model=a.model)
    print(f"replayed train issues through Mubit as {m['run_id']}: {m['n']} scored, {m['errors']} errors; "
          f"exact={_pct(m['exact'])} f1={_pct(m['f1'])} served={m['served']}")


def cmd_correct(a):
    cfg = repo(a.repo)
    labels, rows = load_dataset(cfg)
    issue = next((r for r in rows if r["number"] == a.issue), None)
    if not issue:
        raise SystemExit(f"issue #{a.issue} not in dataset")
    if issue["split"] == "eval":
        # Guidelines go into every eval prompt; an eval issue's labels there would leak the answer.
        raise SystemExit(f"issue #{a.issue} is in the eval split; corrections are only accepted for train issues")
    store = Store()
    predicted = store.latest_prediction(cfg.key, a.issue) or []
    final = [l.strip() for l in a.labels.split(",") if l.strip()]
    line = gl.add_correction(cfg.key, issue, predicted, final)
    print("guideline added:", line)
    guide = gl.load(cfg.key, eval_issues={r["number"] for r in rows if r["split"] == "eval"})
    res = classify(client(), cfg.full_name, labels, issue, guide, model=a.model)
    store.log_call(f"correct-{cfg.key}-{a.issue}", "correct", cfg.key, a.issue, res, truth=final)
    print(f"re-sent through Mubit: pred={res.labels} served_by={res.model_served} req={res.request_id}")


def cmd_report(a):
    store = Store()
    print(render(store, a.repo))
    if a.families:
        print()
        print(render_families(store, a.families))


def repos_with_dataset() -> list[str]:
    return [k for k in REPOS if (DATA_DIR / k / "issues.jsonl").exists()]


def cmd_watch(a):
    repos = a.repos or repos_with_dataset()
    if not repos:
        raise SystemExit("no repo has a dataset; run `triage pull` first")
    while True:
        for key in repos:
            try:
                m = run(repo(key), "eval", Store(), workers=a.workers, model=a.model, verbose=False)
            except (Exception, SystemExit) as e:  # one bad round must not end the schedule
                print(f"watch {key}: round failed: {type(e).__name__}: {e}", file=sys.stderr, flush=True)
                continue
            agree = (m.get("agreement_vs_reference") or {}).get("exact")
            print(f"{m['run_id']}: n={m['n']} errors={m['errors']} exact={_pct(m['exact'])} f1={_pct(m['f1'])} "
                  f"agree_vs_ref={_pct(agree)} p50={m['p50_ms']}ms served={m['served']}",
                  flush=True)
        time.sleep(a.every)


def main(argv=None):
    p = argparse.ArgumentParser(prog="triage")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("pull", help="pull issues and labels from GitHub into data/<repo>/")
    s.add_argument("--repo", required=True, choices=REPOS)
    s.add_argument("--n", type=int, default=300)
    s.set_defaults(fn=cmd_pull)

    for name, fn, help_ in (("eval", cmd_eval, "score the eval split through Mubit"),
                            ("replay", cmd_replay, "send the train split through Mubit")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("--repo", required=True, choices=REPOS)
        s.add_argument("--limit", type=int)
        s.add_argument("--workers", type=int, default=4)
        s.add_argument("--model", default=MODEL)
        s.set_defaults(fn=fn)

    s = sub.add_parser("correct", help="record a maintainer correction and re-send the issue through Mubit")
    s.add_argument("--repo", required=True, choices=REPOS)
    s.add_argument("--issue", type=int, required=True)
    s.add_argument("--labels", required=True, help="comma-separated final labels")
    s.add_argument("--model", default=MODEL)
    s.set_defaults(fn=cmd_correct)

    s = sub.add_parser("report", help="timeline of eval runs")
    s.add_argument("--repo", choices=REPOS)
    s.add_argument("--families", metavar="RUN_ID", help="also show per-label-family scores for a run")
    s.set_defaults(fn=cmd_report)

    s = sub.add_parser("watch", help="re-run the Mubit eval on a schedule")
    s.add_argument("--repos", nargs="+", choices=REPOS, help="default: every repo with a pulled dataset")
    s.add_argument("--every", type=int, default=3600, help="seconds between rounds")
    s.add_argument("--workers", type=int, default=4)
    s.add_argument("--model", default=MODEL)
    s.set_defaults(fn=cmd_watch)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
