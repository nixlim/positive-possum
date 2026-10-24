"""Watchlist pages for the edit pipeline (PIPELINE.md section 4): revisions in an anchored window,
the accounts that made them and the article's sections then and now, one snapshot file per page.

Every request goes through corpus.Session (paced, cached, budgeted) and every query names its
timestamps, so a re-run with the same `as_of` asks the cache the same questions. No model is
called. Bot accounts and their edits are dropped here, before anything downstream sees them."""

from __future__ import annotations

import gzip
import json
import re
import threading
from collections import Counter
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path

from . import corpus as cp
from .config import DATA_DIR
from .reconcile import is_reverting
from .wiki import REVERTED_TAG, editor_kind

PAGES_DIR = DATA_DIR / "pages"
SNAPSHOT_DIR = PAGES_DIR / "snapshots"
WATCHLISTS = {"mps": "Category:UK MPs 2024–present"}
REV_PROPS = "ids|timestamp|user|userid|comment|size|tags|flags"
WIKITEXT_BATCH = 50  # API maximum revids per content query
TOP_ACCOUNTS = 5  # usercontribs costs one request per account, so only a page's most active ones
RECENT_CONTRIBS = 50

_COMMENT = re.compile(r"<!--.*?-->", re.S)
_REF = re.compile(r"<ref[^>]*/>|<ref[^>]*>.*?</ref>", re.S | re.I)
_TEMPLATE = re.compile(r"\{\{[^{}]*\}\}")
_LINK = re.compile(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]")
_TAG = re.compile(r"<[^>]+>")
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*")


def default_as_of() -> datetime:
    return datetime.now(UTC).replace(minute=0, second=0, microsecond=0)


def parse_as_of(text: str) -> datetime:
    t = datetime.fromisoformat(text)
    return t.astimezone(UTC) if t.tzinfo else t.replace(tzinfo=UTC)


def session(budget: int) -> cp.Session:
    return cp.Session(PAGES_DIR, budget)


# ---- pure helpers (tested) -------------------------------------------------


def count_words(wikitext: str) -> int:
    """Prose words: comments, references, templates and markup removed, link text kept."""
    text = _REF.sub(" ", _COMMENT.sub(" ", wikitext))
    n = 1
    while n:  # innermost templates first, so nested ones go too
        text, n = _TEMPLATE.subn(" ", text)
    return len(_WORD.findall(_TAG.sub(" ", _LINK.sub(r"\1", text))))


def split_sections(wikitext: str, sections: list[dict]) -> list[dict]:
    """Words per section, lead first. MediaWiki gives each section's start as a UTF-8 byte
    offset; sections transcluded from templates have none and are not in this wikitext."""
    raw = wikitext.encode()
    heads = [h for h in sections if h.get("byteoffset") is not None]
    starts = [0] + [h["byteoffset"] for h in heads] + [len(raw)]
    titles = [("(lead)", 0)] + [(_TAG.sub("", h["line"]).strip(), int(h["level"])) for h in heads]
    out = []
    for (title, level), (a, b) in zip(titles, pairwise(starts), strict=True):
        text = raw[a:b].decode(errors="ignore")
        if level:  # drop the heading line itself
            text = text.partition("\n")[2]
        out.append({"title": title, "level": level, "words": count_words(text)})
    return out


def build_revisions(window: list[dict], before: dict | None) -> list[dict]:
    """Window revisions oldest first in the snapshot shape. Each size_delta is against the
    revision before it; for the oldest that is `before`, the last revision before the window."""
    if before:
        window = [r for r in window if r["revid"] != before["revid"]]
    prev = before["size"] if before else 0
    out = []
    for r in sorted(window, key=lambda r: (r["timestamp"], r["revid"])):
        tags = r.get("tags", [])
        out.append(
            {
                "revid": r["revid"],
                "parentid": r.get("parentid", 0),
                "timestamp": r["timestamp"],
                "user": r.get("user", ""),
                "userid": r.get("userid", 0),
                "editor_kind": editor_kind(r),
                "comment": r.get("comment", ""),
                "size": r["size"],
                "size_delta": r["size"] - prev,
                "minor": bool(r.get("minor")),
                "tags": tags,
                "reverted": REVERTED_TAG in tags,
                "reverting": is_reverting(r),
            }
        )
        prev = r["size"]
    return out


def drop_bots(revisions: list[dict], accounts: dict[str, dict]) -> tuple[list[dict], dict[str, dict]]:
    bots = {name for name, u in accounts.items() if "bot" in u["groups"]}
    return (
        [r for r in revisions if r["user"] not in bots],
        {name: u for name, u in accounts.items() if name not in bots},
    )


def top_accounts(revisions: list[dict], n: int) -> list[str]:
    """The n accounts (not IP editors) with the most edits, ties by name."""
    counts = Counter(r["user"] for r in revisions if r["user"] and r["editor_kind"] != "anonymous")
    return [name for name, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


# ---- network (through anything with Session.query) -------------------------


def _continued(s, params: dict) -> Iterator[dict]:
    while True:
        data = s.query(params)
        yield data
        if "continue" not in data:
            return
        params = {**params, **data["continue"]}


def watchlist(s, category: str, as_of: datetime) -> list[str]:
    """Article titles in the category. Listed newest-added first from `as_of`, which anchors
    the listing (and its cache key) like every other query here."""
    params = {
        "action": "query",
        "list": "categorymembers",
        "cmtitle": category,
        "cmnamespace": 0,
        "cmlimit": "max",
        "cmsort": "timestamp",
        "cmdir": "older",
        "cmstart": cp.iso(as_of),
    }
    return [m["title"] for data in _continued(s, params) for m in data["query"]["categorymembers"]]


def fetch_window(s, title: str, start: datetime, end: datetime) -> tuple[dict | None, list[dict]]:
    """The page ({pageid, title}, redirects followed) and its revisions in [start, end], newest
    first; (None, []) when there is no such page."""
    params = {
        "action": "query",
        "prop": "revisions",
        "titles": title,
        "redirects": 1,
        "rvprop": REV_PROPS,
        "rvlimit": "max",
        "rvstart": cp.iso(end),
        "rvend": cp.iso(start),
    }
    page, revs = None, []
    for data in _continued(s, params):
        for p in data["query"].get("pages", []):
            if p.get("missing") or p.get("invalid"):
                return None, []
            page = {"pageid": p["pageid"], "title": p["title"]}
            revs.extend(p.get("revisions", []))
    return page, revs


def fetch_before(s, pageid: int, start: datetime) -> dict | None:
    """The newest revision at or before `start`: the article as it was when the window opened."""
    params = {
        "action": "query",
        "prop": "revisions",
        "pageids": pageid,
        "rvprop": REV_PROPS,
        "rvlimit": 1,
        "rvstart": cp.iso(start),
    }
    pages = s.query(params)["query"].get("pages", [])
    revs = pages[0].get("revisions", []) if pages else []
    return revs[0] if revs else None


def fetch_wikitext(s, revids: Iterable[int]) -> dict[int, str]:
    """Wikitext per revision id, paced and cached by the Session (wiki.fetch_contents sends the
    same query uncached). Suppressed or deleted revisions are absent."""
    todo = sorted(set(revids))
    out: dict[int, str] = {}
    for i in range(0, len(todo), WIKITEXT_BATCH):
        params = {
            "action": "query",
            "prop": "revisions",
            "revids": "|".join(map(str, todo[i : i + WIKITEXT_BATCH])),
            "rvprop": "ids|content",
            "rvslots": "main",
        }
        for page in s.query(params)["query"].get("pages", []):
            for rev in page.get("revisions", []):
                content = rev.get("slots", {}).get("main", {}).get("content")
                if content is not None:
                    out[rev["revid"]] = content
    return out


def page_share(s, user: str, title: str, as_of: datetime) -> float | None:
    """Share of the account's last 50 contributions (as of `as_of`) made to this page."""
    params = {
        "action": "query",
        "list": "usercontribs",
        "ucuser": user,
        "uclimit": RECENT_CONTRIBS,
        "ucprop": "title",
        "ucstart": cp.iso(as_of),
    }
    contribs = s.query(params)["query"]["usercontribs"]
    return round(sum(c["title"] == title for c in contribs) / len(contribs), 3) if contribs else None


def fetch_sections(s, rev: dict, texts: dict[int, str]) -> dict | None:
    """Words per section of a revision whose wikitext is in `texts`; None when it is not
    (revision deleted or suppressed), since action=parse on such a revision is an API error
    that would end the whole run."""
    text = texts.get(rev["revid"])
    if text is None:
        return None
    data = s.query({"action": "parse", "oldid": rev["revid"], "prop": "sections"})["parse"]
    return {
        "revid": rev["revid"],
        "timestamp": rev["timestamp"],
        "sections": split_sections(text, data["sections"]),
    }


def ingest_page(s, title: str, as_of: datetime, days: int) -> dict | None:
    """The snapshot of one page, or None when the page does not exist at `as_of`."""
    start = as_of - timedelta(days=days)
    page, window = fetch_window(s, title, start, as_of)
    if page is None:
        return None
    before = fetch_before(s, page["pageid"], start)
    rev_now = max(window, key=lambda r: (r["timestamp"], r["revid"]), default=before)
    if rev_now is None:
        return None
    revisions = build_revisions(window, before)
    accounts = cp.fetch_users(
        s, {r["user"] for r in revisions if r["user"] and r["editor_kind"] != "anonymous"}, groups=True
    )
    revisions, accounts = drop_bots(revisions, accounts)
    users = {name: {**u, "page_share": None} for name, u in accounts.items()}
    for name in top_accounts(revisions, TOP_ACCOUNTS):
        if name in users:
            users[name]["page_share"] = page_share(s, name, page["title"], as_of)
    texts = fetch_wikitext(s, [r["revid"] for r in (before, rev_now) if r])
    return {
        "title": page["title"],
        "pageid": page["pageid"],
        "as_of": as_of.isoformat(),
        "days": days,
        "revisions": revisions,
        "users": users,
        "sections_then": fetch_sections(s, before, texts) if before else None,
        "sections_now": fetch_sections(s, rev_now, texts),
    }


def snapshot_path(pageid: int, root: Path = SNAPSHOT_DIR) -> Path:
    return root / f"{pageid}.json.gz"


def write_snapshot(snap: dict, root: Path = SNAPSHOT_DIR) -> Path:
    path = snapshot_path(snap["pageid"], root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")  # two titles may share a page
    tmp.write_bytes(gzip.compress(json.dumps(snap).encode()))
    tmp.replace(path)
    return path


def load_snapshot(pageid: int, root: Path = SNAPSHOT_DIR) -> dict:
    return json.loads(gzip.decompress(snapshot_path(pageid, root).read_bytes()))


def ingest(s, titles: list[str], as_of: datetime, days: int, root: Path = SNAPSHOT_DIR) -> list[dict]:
    """Snapshot every title (missing pages skipped, redirects to the same page kept once),
    writing each file as soon as it is built."""

    def one(title):
        snap = ingest_page(s, title, as_of, days)
        if snap:
            write_snapshot(snap, root)
        return snap

    return list({snap["pageid"]: snap for snap in cp.parallel(one, titles) if snap}.values())
