"""`triage` command line."""

from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date

from . import guidelines as gl
from . import pages as pg
from .cli_pages import pages_parser, pipeline_parser
from .config import DATA_DIR, MODEL, REPOS, repo
from .github import build_dataset, load_dataset
from .llm import classify, client
from .patrol import Patrol
from .report import render, render_families
from .runner import run
from .server import is_loopback, serve
from .store import Store
from .wiki import build_dataset as build_wiki_dataset


def cmd_pull(a):
    cfg = repo(a.repo)
    path = (build_wiki_dataset if cfg.kind == "wiki" else build_dataset)(cfg, a.n)
    labels, rows = load_dataset(cfg)
    n_eval = sum(r["split"] == "eval" for r in rows)
    n_corr = sum(r["corrected"] for r in rows)
    print(
        f"wrote {path}: {len(rows)} items ({n_eval} eval, {len(rows) - n_eval} train), "
        f"{len(labels)} target labels, {n_corr} with maintainer corrections after first pass"
    )


def cmd_eval(a):
    m = run(repo(a.repo), "eval", Store(), limit=a.limit, workers=a.workers, model=a.model)
    print(json.dumps({k: v for k, v in m.items() if k != "by_family"}, indent=1))


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def cmd_replay(a):
    m = run(repo(a.repo), "replay", Store(), limit=a.limit, workers=a.workers, model=a.model)
    print(
        f"replayed train issues through Mubit as {m['run_id']}: {m['n']} scored, {m['errors']} errors; "
        f"exact={_pct(m['exact'])} f1={_pct(m['f1'])} served={m['served']}"
    )


def cmd_correct(a):
    cfg = repo(a.repo)
    labels, rows = load_dataset(cfg)
    issue = next((r for r in rows if r["number"] == a.issue), None)
    if not issue:
        raise SystemExit(f"issue #{a.issue} not in dataset")
    if issue["split"] == "eval":
        # Guidelines go into every eval prompt; an eval issue's labels there would leak the answer.
        raise SystemExit(
            f"issue #{a.issue} is in the eval split; corrections are only accepted for train issues"
        )
    store = Store()
    predicted = store.latest_prediction(cfg.key, a.issue) or []
    final = [lab.strip() for lab in a.labels.split(",") if lab.strip()]
    line = gl.add_correction(cfg.key, issue, predicted, final)
    print("guideline added:", line)
    guide = gl.load(cfg.key, eval_issues={r["number"] for r in rows if r["split"] == "eval"})
    res = classify(client(), cfg.full_name, labels, issue, guide, model=a.model, kind=cfg.kind)
    store.log_call(f"correct-{cfg.key}-{a.issue}", "correct", cfg.key, a.issue, res, truth=final)
    print(f"re-sent through Mubit: pred={res.labels} served_by={res.model_served} req={res.request_id}")


def cmd_report(a):
    store = Store()
    print(render(store, a.repo))
    if a.families:
        print()
        print(render_families(store, a.families))


def cmd_provenance(a):
    from . import provenance as pv

    # Read every step: classify agreement needs the extract rows its calls point to.
    rep = pv.report(pv.read_calls(), a.baseline, a.hours)
    if a.step:
        rep = {a.step: rep[a.step]} if a.step in rep else {}
    print(json.dumps(rep, indent=1) if a.json else pv.render(rep))


def _provenance_parser(sub) -> None:
    s = sub.add_parser(
        "provenance",
        help="per step: provider test, errors, shift and probe agreement from the log (no calls)",
    )
    s.add_argument("--step", help="one step (calls.repo), e.g. enwiki or edit-classify; default: all")
    s.add_argument("--hours", type=float, default=24, help="recent window compared with the baseline")
    s.add_argument(
        "--baseline", type=int, default=200, help="first N answered provider-served calls of a step"
    )
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_provenance)


def cmd_corpus(a):
    from .corpus_build import Spec, build

    m = build(Spec(a.windows, a.diffs, a.budget, a.liftwing, a.seed, a.anchor))
    print(json.dumps({k: m[k] for k in ("counts", "sampling", "requests_sent", "cache_hits")}, indent=1))


def cmd_reconcile(a):
    from .reconcile import outcome_stats, reconcile
    from .wiki import _client

    cfg = repo(a.repo)
    if cfg.kind != "wiki":
        raise SystemExit("reconcile only applies to wiki repos")
    store = Store()
    with _client() as http:
        for written in reconcile(store, cfg, http):
            for o in written:
                lead = o["lead_s"]
                when = "" if lead is None else f"{abs(lead) // 60} min {'ahead' if lead >= 0 else 'behind'}"
                print(f"{o['issue']}: we said {o['verdict']}, wikipedia says {o['truth']} {when}")
    print(json.dumps(outcome_stats(store.outcomes(cfg.key))))


def cmd_serve(a):
    cfg = repo(a.repo)
    if cfg.kind != "wiki":
        raise SystemExit("the patrol desk follows a wiki; use --repo enwiki")
    print(f"patrol desk on http://{a.host}:{a.port}  (one Mubit call every {a.rate}s)")
    serve(Patrol(cfg, model=a.model, min_interval_s=a.rate), a.host, a.port, a.allow_remote)


def cmd_api(a):
    from .api import serve as serve_api
    from .watch import Options, Service

    if not is_loopback(a.host):
        raise SystemExit(f"--host {a.host} is not loopback; the watchlist API has no authentication")
    titles = [t.strip() for t in a.titles.split("|") if t.strip()] if a.titles else None
    opts = Options(
        watchlist=None if titles else a.watchlist,
        titles=titles,
        call_budget=a.call_budget,
        min_interval=a.min_interval,
        live_reserve=a.live_reserve,
        request_budget=a.request_budget,
        wiki_live_reserve=a.wiki_live_reserve,
        live=not a.no_live,
        model=a.model,
    )
    origins = [f"http://{h}:{a.port}" for h in ("127.0.0.1", "localhost")] + a.extension_origin
    print(f"watchlist dashboard on http://{a.host}:{a.port}  (at most {a.call_budget} Mubit calls)")
    serve_api(Service(opts, DATA_DIR, client), a.host, a.port, origins)


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
            print(
                f"{m['run_id']}: n={m['n']} errors={m['errors']} exact={_pct(m['exact'])} f1={_pct(m['f1'])} "
                f"agree_vs_ref={_pct(agree)} p50={m['p50_ms']}ms served={m['served']}",
                flush=True,
            )
        time.sleep(a.every)


def _corpus_parser(sub) -> None:
    s = sub.add_parser(
        "corpus",
        help="build the local edit corpus (Wikipedia API only, no LLM) for notebooks/edit_intent.ipynb",
    )
    s.add_argument("--windows", type=int, default=24, help="one-hour windows over days 4-27 ago")
    s.add_argument("--diffs", type=int, default=10_000, help="edits in the diff sample")
    s.add_argument("--budget", type=int, default=15_000, help="maximum HTTP requests sent")
    s.add_argument("--liftwing", type=int, default=400, help="test edits scored by Wikimedia's model")
    s.add_argument("--seed", type=int, default=0)
    s.add_argument("--anchor", type=date.fromisoformat, help="YYYY-MM-DD; default: kept from the first run")
    s.set_defaults(fn=cmd_corpus)


def _serve_parser(sub) -> None:
    s = sub.add_parser("serve", help="live patrol desk: Wikimedia stream -> Mubit -> browser")
    s.add_argument("--repo", default="enwiki", choices=REPOS)
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument(
        "--allow-remote",
        action="store_true",
        help="serve on a non-loopback host; the desk has no authentication",
    )
    s.add_argument("--port", type=int, default=8321)
    s.add_argument("--rate", type=float, default=2.0, help="minimum seconds between Mubit calls")
    s.add_argument("--model", default=MODEL)
    s.set_defaults(fn=cmd_serve)


def _watch_parser(sub) -> None:
    s = sub.add_parser("watch", help="re-run the Mubit eval on a schedule")
    s.add_argument("--repos", nargs="+", choices=REPOS, help="default: every repo with a pulled dataset")
    s.add_argument("--every", type=int, default=3600, help="seconds between rounds")
    s.add_argument("--workers", type=int, default=4)
    s.add_argument("--model", default=MODEL)
    s.set_defaults(fn=cmd_watch)


def _api_parser(sub) -> None:
    s = sub.add_parser(
        "api", help="watchlist dashboard and extension API: page analytics, flags, briefs, live feed"
    )
    src = s.add_mutually_exclusive_group()
    src.add_argument("--watchlist", default="mps", choices=pg.WATCHLISTS)
    src.add_argument("--titles", help="A|B: these pages instead of the watchlist")
    s.add_argument("--host", default="127.0.0.1", help="loopback only")
    s.add_argument("--port", type=int, default=8322)
    s.add_argument("--call-budget", type=int, default=300, help="Mubit calls for the process lifetime")
    s.add_argument("--min-interval", type=float, default=2.0, help="minimum seconds between Mubit calls")
    s.add_argument("--live-reserve", type=int, default=60, help="Mubit calls kept for the live feed")
    s.add_argument("--request-budget", type=int, default=2000, help="Wikipedia requests at most")
    s.add_argument("--wiki-live-reserve", type=int, default=300, help="Wikipedia requests kept for live")
    s.add_argument(
        "--extension-origin",
        action="append",
        default=[],
        help="an origin allowed to POST, e.g. chrome-extension://<id> (repeatable)",
    )
    s.add_argument("--no-live", action="store_true", help="do not follow the Wikimedia stream")
    s.add_argument("--model", default=MODEL)
    s.set_defaults(fn=cmd_api)


def main(argv=None):
    p = argparse.ArgumentParser(prog="triage")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("pull", help="pull issues (GitHub) or edits (wiki) into data/<repo>/")
    s.add_argument("--repo", required=True, choices=REPOS)
    s.add_argument("--n", type=int, default=300)
    s.set_defaults(fn=cmd_pull)

    for name, fn, help_ in (
        ("eval", cmd_eval, "score the eval split through Mubit"),
        ("replay", cmd_replay, "send the train split through Mubit"),
    ):
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
    _provenance_parser(sub)

    s = sub.add_parser(
        "reconcile",
        help="settle live verdicts against what Wikipedia did next (offline: while `serve` runs, "
        "its own reconciler does this and also marks and corrects the desk cards)",
    )
    s.add_argument("--repo", default="enwiki", choices=REPOS)
    s.set_defaults(fn=cmd_reconcile)
    _corpus_parser(sub)
    _serve_parser(sub)
    pages_parser(sub)
    pipeline_parser(sub)
    _api_parser(sub)
    _watch_parser(sub)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()
