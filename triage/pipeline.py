"""Revisions through edit-extract then edit-classify, and the page brief on demand.

Every call, failures and retries included, is logged before anything reads it, and each classify
row names the extract row (and who served it) that fed it. Normal runs resume from non-probe rows
only, and send only the steps that are not final (edit_state, PIPELINE.md section 7.4). A probe
run (section 8) re-sends every revision: extract with the same message, classify fed the
revision's baseline extract (its first answered non-probe extract row), and, if chained, one more
classify fed the fresh probe extract. Every call is training data and counts toward Mubit's cap:
one retry on an unparseable reply, a call budget, a pace (caller.py)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

from anthropic import Anthropic

from . import edit_state as es
from . import pages, steps
from .caller import Caller
from .classification import (
    RULE_LABEL,
    classification,
    extract_of,
    parse_brief,
    parse_classify,
    parse_extract,
    skipped,
)
from .config import MODEL
from .corpus import Session, fetch_users, ts
from .diffs import edit_diff
from .edit_state import NOT_PROBE, PROBE
from .llm import CallResult
from .store import Store
from .wiki import CONTENT_BATCH, editor_kind

REV_PROPS = "ids|timestamp|user|userid|comment|size|tags|flags"
WIKI_BUDGET = 1000  # Wikipedia requests per `pipeline run` (wikitext: one per 50 revisions)


def _answered(got: tuple[CallResult, int] | None) -> tuple[dict | None, str | None]:
    """(the row in Store.answered's shape, None) for an answered call, else (None, error)."""
    if got is None:
        return None, "budget spent"
    res, row_id = got
    if res.error:
        return None, res.error
    return {"id": row_id, "model_served": res.model_served, "labels": res.labels, "extra": res.extra}, None


def is_bot(rev: dict) -> bool:
    return bool(rev.get("bot")) or "bot" in (rev.get("account") or {}).get("groups", [])


def account_facts(rev: dict) -> dict:
    """Account context for classify: age at the edit (from registration), edit count and page
    share as read at ingest. None when unknown (IP editors have no account)."""
    acc = rev.get("account") or {}
    reg = acc.get("registration")
    return {
        "account_age_days": (ts(rev["timestamp"]) - ts(reg)).days if reg else None,
        "edit_count": acc.get("editcount"),
        "page_share": acc.get("page_share"),
    }


def _extract(caller: Caller, rev: dict, texts: dict[int, str], model: str) -> tuple[dict | None, str | None]:
    old = texts.get(rev["parentid"]) if rev.get("parentid") else ""
    new = texts.get(rev["revid"])
    if old is None or new is None:
        # Absent from a complete reply: deleted or suppressed, so it can never be diffed. A probe
        # records nothing, since probe activity never changes an edit's state.
        if caller.kind != PROBE:
            caller.store.log_unavailable(rev["revid"], "revision" if new is None else "parent")
        return None, es.WIKITEXT_UNAVAILABLE
    diff, measured = edit_diff(old, new)
    measured["size_delta"] = rev["size_delta"]  # so a classification can be rebuilt from the log
    # Account facts are fixed with the extract, so a probe classify fed this row sends the same input.
    facts = {"measured": measured, "account": account_facts(rev)}
    req = steps.build_request(steps.EXTRACT, model, steps.extract_message(rev, diff, measured), rev["revid"])
    return _answered(caller.call(steps.EXTRACT, rev["revid"], req, parse_extract, facts))


def _classify(caller: Caller, rev: dict, ext: dict, model: str) -> tuple[dict | None, str | None]:
    """Classify fed by the extract row `ext`; (None, None) without a call when the skip rule holds."""
    if skipped(ext["extra"]):
        return None, None
    measured, account = ext["extra"]["measured"], ext["extra"]["account"]
    user = steps.classify_message(rev, measured, account, extract_of(ext["extra"]))
    chain = {"extract_call_id": ext["id"], "extract_served": ext["model_served"]}
    req = steps.build_request(steps.CLASSIFY, model, user, rev["revid"])
    return _answered(caller.call(steps.CLASSIFY, rev["revid"], req, parse_classify, chain))


def _result(rev: dict) -> dict:
    return {
        "revid": rev["revid"],
        "title": rev["title"],
        "extract": None,
        "classification": None,
        "served": {"extract": None, "classify": None},
        "error": None,
    }


def _resume(caller: Caller, rev: dict, texts: dict, model: str, log: es.Log) -> dict:
    """A normal run: the steps that are not final are sent, a final one is reused; an errored
    edit is not sent at all. A skip-rule label is stored in place of a classify row."""
    revid, result = rev["revid"], _result(rev)
    state, err = es.edit_state(log, revid), None
    ext, cls = log.extracted.get(revid), log.classified.get(revid)
    if ext is None and state == es.OPEN:
        ext, err = _extract(caller, rev, texts, model)
    if ext is not None:
        result["extract"], result["served"]["extract"] = extract_of(ext["extra"]), ext["model_served"]
        if state == es.OPEN:
            cls, err = _classify(caller, rev, ext, model)
            if cls is None and err is None:
                measured = ext["extra"]["measured"]
                caller.store.log_rule_label(revid, RULE_LABEL["label"], measured, ext["id"])
        if err is None and state != es.ERRORED:
            result["classification"] = classification(ext["extra"], cls["extra"] if cls else None)
            result["served"]["classify"] = cls["model_served"] if cls else None
    result["error"] = es.errored_reason(log, revid) if state == es.ERRORED else err
    return result


def _probe(caller: Caller, rev: dict, texts: dict, model: str, base: dict | None, chained: bool) -> dict:
    """A probe: `extract` is the fresh probe extract, `classification` the classify fed the
    baseline extract `base` (identical input), `chained` the one fed the fresh extract."""
    result = _result(rev) | {"chained": None}
    fresh, err = _extract(caller, rev, texts, model)
    errors = [err]
    if fresh is not None:
        result["extract"], result["served"]["extract"] = extract_of(fresh["extra"]), fresh["model_served"]
    if base is None:
        errors.append("no baseline extract")
    else:
        cls, err = _classify(caller, rev, base, model)
        errors.append(err)
        if err is None:
            result["classification"] = classification(base["extra"], cls["extra"] if cls else None)
            result["served"]["classify"] = cls["model_served"] if cls else None
    if chained and fresh is not None:
        cls, err = _classify(caller, rev, fresh, model)
        errors.append(err)
        if err is None:
            result["chained"] = classification(fresh["extra"], cls["extra"] if cls else None)
    result["error"] = next((e for e in errors if e), None)
    return result


def run(
    caller: Caller,
    revisions: list[dict],
    fetch_texts: Callable[[list[int]], dict[int, str]],
    model: str = MODEL,
    chained: bool = False,
    log: Callable[[str], None] = print,
) -> list[dict]:
    """Revision dicts in snapshot format plus `title` and `account` (the snapshot's users
    entry, or None) through extract then classify, within the caller's budget and pace; a probe
    run when the caller's kind is `probe` (`chained` only applies there). Returns one result
    per non-bot revision, in order. At most four calls per edit (each step and its retry); a
    failed wikitext fetch raises before any call and records nothing."""
    store = caller.store
    revs = [r for r in revisions if not is_bot(r)]
    probe = caller.kind == PROBE
    if probe:
        baseline = store.answered(steps.EXTRACT, skip_kinds=NOT_PROBE, first=True)
        state, todo = es.Log(), revs
    else:
        state = es.read(store)
        todo = [
            r
            for r in revs
            if r["revid"] not in state.extracted and es.edit_state(state, r["revid"]) == es.OPEN
        ]
    texts = fetch_texts([i for r in todo for i in (r["revid"], r.get("parentid")) if i]) if todo else {}
    out = []
    for rev in revs:
        revid = rev["revid"]
        if probe:
            result = _probe(caller, rev, texts, model, baseline.get(revid), chained)
        else:
            result = _resume(caller, rev, texts, model, state)
        if result["error"]:
            log(f"{revid}: {result['error']}")
        out.append(result)
    return out


def logged_edits(store: Store, revids: list[int]) -> dict[int, dict]:
    """{revid: {extract, measured, classification}} from the latest non-probe rows, in the
    shape page_analytics.analyse takes; revisions never extracted are absent."""
    extracted = store.answered(steps.EXTRACT, skip_kinds=NOT_PROBE)
    classified = store.answered(steps.CLASSIFY, skip_kinds=NOT_PROBE)
    out = {}
    for revid in revids:
        if revid not in extracted:
            continue
        ext, cls = extracted[revid]["extra"], classified.get(revid)
        out[revid] = {
            "extract": extract_of(ext),
            "measured": ext["measured"],
            "classification": classification(ext, cls["extra"] if cls else None),
        }
    return out


def snapshots(root: Path) -> Iterator[dict]:
    """Every snapshot file pages.write_snapshot wrote under `root`."""
    for path in sorted(root.glob("*.json.gz")):
        yield pages.load_snapshot(int(path.name.removesuffix(".json.gz")), root)


def load_snapshots(root: Path, titles: set[str] | None = None) -> list[dict]:
    """Revisions of the snapshot files, each with its page's title, pageid and the snapshot's
    users entry as `account`."""
    out = []
    for snap in snapshots(root):
        if titles and snap["title"] not in titles:
            continue
        users = snap.get("users") or {}
        out += [
            {**r, "title": snap["title"], "pageid": snap["pageid"], "account": users.get(r.get("user"))}
            for r in snap["revisions"]
        ]
    return out


def fetch_revisions(session: Session, revids: list[int]) -> list[dict]:
    """Minimal snapshot-format revision dicts for up to 50 mainspace revids: one revisions
    query, one for the parents' sizes, and the accounts (groups too, to drop bots)."""
    if len(revids) > CONTENT_BATCH:
        raise SystemExit(f"at most {CONTENT_BATCH} revids at a time")

    def revisions(ids: list[int], props: str) -> list[dict]:
        q = {"action": "query", "prop": "revisions", "revids": "|".join(map(str, ids)), "rvprop": props}
        found = session.query(q)["query"].get("pages", [])
        return [
            {**r, "title": p["title"], "pageid": p["pageid"]}
            for p in found
            if p.get("ns") == 0
            for r in p.get("revisions", [])
        ]

    revs = revisions(revids, REV_PROPS)
    parents = sorted({r["parentid"] for r in revs if r.get("parentid")})
    sizes = {r["revid"]: r["size"] for r in revisions(parents, "ids|size")} if parents else {}
    names = {r["user"] for r in revs if r.get("user") and editor_kind(r) != "anonymous"}
    users = fetch_users(session, names, groups=True)
    return [
        {
            "revid": r["revid"],
            "parentid": r.get("parentid", 0),
            "timestamp": r["timestamp"],
            "user": r.get("user"),
            "userid": r.get("userid", 0),
            "editor_kind": editor_kind(r),
            "comment": r.get("comment") or "",
            "size": r["size"],
            "size_delta": r["size"] - sizes.get(r.get("parentid"), 0),
            "minor": bool(r.get("minor")),
            "tags": r.get("tags", []),
            "title": r["title"],
            "pageid": r["pageid"],
            "account": users.get(r.get("user")),
        }
        for r in revs
    ]


def brief(
    client: Anthropic,
    store: Store,
    page_analysis: dict,
    flagged_edits: list[dict],
    model: str = MODEL,
    caller: Caller | None = None,
) -> CallResult | None:
    """The page brief (at most two calls: one retry if the reply is empty), through `caller`
    when given (its budget, pace and run_id). The logged result, `extra["brief_md"]` holding the
    markdown with the closing line ensured; None when the budget was spent."""
    pageid = page_analysis["pageid"]
    caller = caller or Caller(client, store, "brief", max_calls=2)
    user = steps.brief_message(page_analysis, flagged_edits)
    req = steps.build_request(steps.BRIEF, model, user, pageid)
    got = caller.call(steps.BRIEF, pageid, req, parse_brief)
    return got[0] if got else None
