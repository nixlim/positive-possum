"""`triage pages` and `triage pipeline`: the watchlist commands, kept apart from cli.py for size.

The handlers read DATA_DIR, client and Store through the cli module when they run, so whatever
the cli module holds (tests patch it) is what they use."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path

from . import pages as pg
from .config import MODEL


def _cli():
    from . import cli

    return cli


def cmd_pages(a):
    from . import candidates as cd
    from .page_analytics import analyse

    as_of = pg.parse_as_of(a.as_of) if a.as_of else pg.default_as_of()
    s = pg.session(a.budget)
    titles = a.titles.split("|") if a.titles else pg.watchlist(s, pg.WATCHLISTS[a.watchlist], as_of)
    snaps = pg.ingest(s, titles, as_of, a.days)
    n_revs = sum(len(x["revisions"]) for x in snaps)
    print(
        f"{len(snaps)}/{len(titles)} pages, {n_revs} human revisions in {a.days}d to {as_of.isoformat()}; "
        f"requests sent {s.sent.total()}, cache hits {s.hits}"
    )
    if a.pages_cmd == "candidates":
        rows = cd.rank([analyse(x) for x in snaps])
        path = cd.write(rows, as_of.isoformat(), a.days)
        for r in rows[: a.top]:
            print(f"{r['rank']:>3}. {r['score']:5.1f}  {r['title']}: {r['reason']}")
        print(f"wrote {path}")


def cmd_pipeline_run(a):
    from . import pipeline
    from .corpus import Session

    cli = _cli()
    if a.chained and a.kind != pipeline.PROBE:
        raise SystemExit("--chained applies to --kind probe only")
    session = Session(cli.DATA_DIR / "pages", pipeline.WIKI_BUDGET)
    if a.revids:
        revs = pipeline.fetch_revisions(session, [int(x) for x in a.revids.split(",") if x.strip()])
    else:
        titles = {t.strip() for t in a.titles.split("|")} if a.titles else None
        revs = pipeline.load_snapshots(a.snapshots or cli.DATA_DIR / "pages" / "snapshots", titles)
    if not revs:
        raise SystemExit("no revisions to run")
    caller = pipeline.Caller(cli.client(), cli.Store(), a.kind, a.max_calls, a.rate)
    results = pipeline.run(
        caller, revs, partial(pg.fetch_wikitext, session), model=a.model, chained=a.chained
    )
    for r in results:
        print(json.dumps(r, ensure_ascii=False))
    print(
        f"{len(results)} revisions; {a.max_calls - caller.left} Mubit calls;"
        f" {session.sent.total()} wiki requests"
    )


def cmd_pipeline_brief(a):
    from . import page_analytics, pipeline

    cli = _cli()
    root = a.snapshots or cli.DATA_DIR / "pages" / "snapshots"
    snap = next((s for s in pipeline.snapshots(root) if s["title"] == a.title), None)
    if snap is None:
        raise SystemExit(f"no snapshot of {a.title!r} under {root}; run `triage pages ingest` first")
    store = cli.Store()
    edits = pipeline.logged_edits(store, [r["revid"] for r in snap["revisions"]])
    analysis = page_analytics.analyse(snap, edits)
    flagged = [
        {**r, "classification": edits[r["revid"]]["classification"]}
        for r in snap["revisions"]
        if page_analytics.is_flagged((edits.get(r["revid"]) or {}).get("classification"))
    ]
    res = pipeline.brief(cli.client(), store, analysis, flagged, model=a.model)
    if res is None or res.error:
        raise SystemExit(f"brief failed: {res.error if res else 'budget spent'}")
    print(res.extra["brief_md"])


def pages_parser(sub) -> None:
    s = sub.add_parser(
        "pages", help="watchlist page snapshots and demo candidates (Wikipedia API only, no LLM)"
    )
    pages_sub = s.add_subparsers(dest="pages_cmd", required=True)
    for name, help_ in (
        ("ingest", "snapshot revisions, editors and sections of each page"),
        ("candidates", "ingest, then rank pages by deterministic signals"),
    ):
        ps = pages_sub.add_parser(name, help=help_)
        ps.add_argument("--titles", help="pipe-separated titles instead of the watchlist")
        ps.add_argument("--watchlist", default="mps", choices=pg.WATCHLISTS)
        ps.add_argument("--as-of", help="ISO time closing the window; default: now, rounded down to the hour")
        ps.add_argument("--days", type=int, default=90)
        ps.add_argument("--budget", type=int, default=10_000, help="maximum HTTP requests sent")
        if name == "candidates":
            ps.add_argument("--top", type=int, default=25)
        ps.set_defaults(fn=cmd_pages)


def pipeline_parser(sub) -> None:
    s = sub.add_parser("pipeline", help="watchlist pipeline: edits through extract and classify")
    steps_ = s.add_subparsers(dest="pipeline_cmd", required=True)
    s = steps_.add_parser("run", help="run revisions through edit-extract then edit-classify (resumes)")
    src = s.add_mutually_exclusive_group()
    src.add_argument("--titles", help="A|B: only these pages of the snapshots")
    src.add_argument("--revids", help="1,2,3: these revisions, fetched from the API (at most 50)")
    s.add_argument("--snapshots", type=Path, help="default: data/pages/snapshots")
    s.add_argument("--max-calls", type=int, default=50, help="Mubit calls at most, retries included")
    s.add_argument("--rate", type=float, default=1.0, help="minimum seconds between Mubit calls")
    s.add_argument("--kind", default="pipeline", choices=("pipeline", "live", "probe"))
    s.add_argument(
        "--chained", action="store_true", help="probe only: also classify from the fresh probe extract"
    )
    s.add_argument("--model", default=MODEL)
    s.set_defaults(fn=cmd_pipeline_run)
    s = steps_.add_parser("brief", help="brief one page from its snapshot and the logged classifications")
    s.add_argument("--title", required=True)
    s.add_argument("--snapshots", type=Path, help="default: data/pages/snapshots")
    s.add_argument("--model", default=MODEL)
    s.set_defaults(fn=cmd_pipeline_brief)
