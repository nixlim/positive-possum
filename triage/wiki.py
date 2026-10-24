"""Pull recent human edits from a MediaWiki wiki into a dataset.

Ground truth is free: MediaWiki tags every revision that patrollers undo with
`mw-reverted`. An edit older than MIN_AGE_HOURS without that tag is "keep",
with it "revert". No one on our side labels anything.

Dataset rows use the same shape as GitHub issues (number, title, body, labels,
split) so eval, replay, correct, watch and report work unchanged. `number` is
the revision id and `body` holds the editor type, edit summary and a unified
diff of the wikitext."""

from __future__ import annotations

import difflib
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .config import DATA_DIR, RepoConfig
from .github import assign_split, load_dataset

USER_AGENT = "mubit-triage/0.1 (hackathon harness; riabtchuk@gmail.com)"
MIN_AGE_HOURS = 12  # most reverts land within hours; younger edits have unsettled truth
MAX_AGE_DAYS = 29  # the recentchanges table only keeps 30 days
REVERTED_TAG = "mw-reverted"
# Edits that themselves undo something: trivially "keep", and their summaries give the
# game away ("Reverted edits by ..."). The task is about content edits.
REVERTING_TAGS = {"mw-rollback", "mw-undo", "mw-manual-revert"}
CONTENT_BATCH = 50  # revids per revisions query (API maximum for users)
# Wikimedia etiquette: back off on 429/503 and on the API's `maxlag` error, honouring Retry-After.
RETRY_STATUS = {429, 503}
MAX_ATTEMPTS = 5
MAX_RETRY_WAIT_S = 60.0

LABELS = [
    {"name": "keep", "description": "the edit stays; patrollers would not undo it"},
    {"name": "revert", "description": "patrollers would undo this edit"},
]


def _client() -> httpx.Client:
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=60)


def retry_wait(r: httpx.Response, attempt: int) -> float | None:
    """Seconds to wait before re-sending, or None when the response is final. A lagged database
    answers `maxlag` with HTTP 200 and the error code in the MediaWiki-API-Error header."""
    if r.status_code not in RETRY_STATUS and r.headers.get("MediaWiki-API-Error") != "maxlag":
        return None
    try:
        wait = float(r.headers.get("Retry-After", ""))
    except ValueError:
        wait = 2.0**attempt
    return min(max(wait, 1.0), MAX_RETRY_WAIT_S)


def _send(client: httpx.Client, method: str, url: str, **kw) -> httpx.Response:
    """One request with Wikimedia etiquette: retried after Retry-After on 429, 503 and maxlag."""
    attempt = 0
    while True:
        r = client.request(method, url, **kw)
        wait = retry_wait(r, attempt)
        if wait is None or attempt >= MAX_ATTEMPTS - 1:
            return r
        time.sleep(wait)
        attempt += 1


def _api(client: httpx.Client, host: str, params: dict) -> dict:
    r = _send(
        client, "GET", f"https://{host}/w/api.php", params={**params, "format": "json", "formatversion": 2}
    )
    r.raise_for_status()
    data = r.json()
    if "error" in data:
        raise SystemExit(f"{host} API error: {data['error']}")
    return data


# ---- pure helpers (tested) -------------------------------------------------


def label_for(tags: list[str]) -> str:
    return "revert" if REVERTED_TAG in tags else "keep"


def is_content_edit(rc: dict) -> bool:
    return bool(rc.get("old_revid")) and not (set(rc.get("tags", [])) & REVERTING_TAGS)


def select_edits(reverted: list[dict], others: list[dict], n: int) -> list[dict]:
    """A balanced sample: up to n/2 reverted and n/2 kept content edits, newest first.
    Reverts are a few percent of edits, so an unbalanced pull would teach "keep"."""
    rev = [rc for rc in reverted if is_content_edit(rc)][: n // 2]
    keep = [rc for rc in others if is_content_edit(rc) and REVERTED_TAG not in rc.get("tags", [])][
        : n - len(rev)
    ]
    seen: set[int] = set()
    out = []
    for rc in sorted(rev + keep, key=lambda r: r["timestamp"], reverse=True):
        if rc["revid"] not in seen:
            seen.add(rc["revid"])
            out.append(rc)
    return out


def diff_text(old: str, new: str, max_chars: int) -> str:
    lines = list(difflib.unified_diff(old.splitlines(), new.splitlines(), n=1, lineterm=""))[2:]
    text = "\n".join(lines) or "(no textual change)"
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[truncated]"
    return text


def editor_kind(rc: dict) -> str:
    """Who made the edit: temporary, anonymous or registered. Logged-out editors now get
    temporary accounts, which the API flags `temp` and gives a real userid, so a userid
    alone does not mean registered. IP editors carry `anon` and userid 0."""
    if rc.get("temp"):
        return "temporary"
    if rc.get("anon") or not rc.get("userid"):
        return "anonymous"
    return "registered"


EDITOR_LABELS = {
    "temporary": "temporary account (logged out)",
    "anonymous": "anonymous (IP)",
    "registered": "registered account",
}


def edit_body(rc: dict, old: str, new: str, max_chars: int) -> str:
    editor = EDITOR_LABELS[editor_kind(rc)]
    delta = rc.get("newlen", 0) - rc.get("oldlen", 0)
    return (
        f"Editor: {editor}\n"
        f"Edit summary: {rc.get('comment') or '(none)'}\n"
        f"Size change: {delta:+d} bytes\n\n"
        f"Diff:\n{diff_text(old, new, max_chars)}"
    )


# ---- network ---------------------------------------------------------------


def fetch_recent(client: httpx.Client, host: str, n: int, tag: str | None = None) -> list[dict]:
    """Human mainspace edits older than MIN_AGE_HOURS, newest first, optionally with a tag."""
    start = datetime.now(UTC) - timedelta(hours=MIN_AGE_HOURS)
    end = datetime.now(UTC) - timedelta(days=MAX_AGE_DAYS)
    params = {
        "action": "query",
        "list": "recentchanges",
        "rctype": "edit",
        "rcshow": "!bot",
        "rcnamespace": 0,
        "rcprop": "ids|title|timestamp|user|userid|comment|sizes|tags|flags",
        "rclimit": min(500, max(n, 50)),
        "rcstart": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rcend": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    if tag:
        params["rctag"] = tag
    out: list[dict] = []
    while len(out) < n:
        data = _api(client, host, params)
        out.extend(data["query"]["recentchanges"])
        cont = data.get("continue")
        if not cont:
            break
        params = {**params, **cont}
    return out[:n]


def fetch_contents(client: httpx.Client, host: str, revids: list[int]) -> dict[int, str]:
    """Wikitext per revision id; suppressed or deleted revisions are absent."""
    out: dict[int, str] = {}
    for i in range(0, len(revids), CONTENT_BATCH):
        batch = revids[i : i + CONTENT_BATCH]
        data = _api(
            client,
            host,
            {
                "action": "query",
                "prop": "revisions",
                "revids": "|".join(map(str, batch)),
                "rvprop": "ids|content",
                "rvslots": "main",
            },
        )
        for page in data["query"].get("pages", []):
            for rev in page.get("revisions", []):
                content = rev.get("slots", {}).get("main", {}).get("content")
                if content is not None:
                    out[rev["revid"]] = content
    return out


def build_dataset(cfg: RepoConfig, n: int, data_dir: Path = DATA_DIR) -> Path:
    out_dir = data_dir / cfg.key
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "labels.json").write_text(json.dumps(LABELS, indent=1))
    host = cfg.full_name
    with _client() as client:
        # Reverts are rare, so ask for them by tag and fish the kept edits from the plain list.
        reverted = fetch_recent(client, host, n, tag=REVERTED_TAG)
        others = fetch_recent(client, host, n * 2)
        edits = select_edits(reverted, others, n)
        ids = sorted({rc["old_revid"] for rc in edits} | {rc["revid"] for rc in edits})
        texts = fetch_contents(client, host, ids)
    edits = [rc for rc in edits if rc["revid"] in texts and rc["old_revid"] in texts]
    existing = (
        {r["number"]: r["split"] for r in load_dataset(cfg, data_dir)[1]}
        if (out_dir / "issues.jsonl").exists()
        else {}
    )
    split = assign_split(cfg.key, [rc["revid"] for rc in edits], existing)
    rows = [
        {
            "number": rc["revid"],
            "title": rc["title"],
            "body": edit_body(rc, texts[rc["old_revid"]], texts[rc["revid"]], cfg.body_max_chars),
            "created_at": rc["timestamp"],
            "state": "reverted" if label_for(rc.get("tags", [])) == "revert" else "live",
            "url": f"https://{host}/w/index.php?diff={rc['revid']}",
            "labels": [label_for(rc.get("tags", []))],
            "first_pass": [],
            "corrected": False,
            "split": split[rc["revid"]],
        }
        for rc in edits
    ]
    path = out_dir / "issues.jsonl"
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path
