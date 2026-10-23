"""Pull issues, curated labels and label events from GitHub into a dataset."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from .config import DATA_DIR, MIN_AGE_DAYS, RepoConfig

API = "https://api.github.com"
FIRST_PASS_WINDOW = timedelta(minutes=10)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_BLANK_RE = re.compile(r"\n{3,}")


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        return tok
    out = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True)
    if out.returncode != 0 or not out.stdout.strip():
        raise SystemExit("set GITHUB_TOKEN or log in with `gh auth login`")
    return out.stdout.strip()


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=API,
        headers={"Authorization": f"Bearer {_token()}", "Accept": "application/vnd.github+json"},
        timeout=30,
    )


def _paginate(client: httpx.Client, path: str, params: dict):
    url: str | None = path
    while url:
        r = client.get(url, params=params if url == path else None)
        r.raise_for_status()
        yield from r.json()
        url = r.links.get("next", {}).get("url")


# ---- pure helpers (tested) -------------------------------------------------


def clean_body(body: str | None, max_chars: int) -> str:
    text = _COMMENT_RE.sub("", body or "")
    text = _BLANK_RE.sub("\n\n", text).strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[truncated]"
    return text


def filter_labels(cfg: RepoConfig, labels: list[dict]) -> list[dict]:
    """Keep only target labels, as {name, description}, sorted by name."""
    out = [
        {"name": lab["name"], "description": (lab.get("description") or "").strip()}
        for lab in labels
        if cfg.is_target(lab["name"])
    ]
    return sorted(out, key=lambda lab: lab["name"])


def is_bot(actor: str | None) -> bool:
    return actor is None or actor.endswith("[bot]")


def first_pass(events: list[dict], targets: set[str]) -> set[str]:
    """Target labels after the first human labeler's batch of events.

    The batch is every label event by that actor within FIRST_PASS_WINDOW of
    their first one. Bot events are ignored. Empty set when no human labelled.
    """
    labelled = [e for e in events if e["event"] in ("labeled", "unlabeled") and not is_bot(e["actor"])]
    if not labelled:
        return set()
    actor = labelled[0]["actor"]
    start = _ts(labelled[0]["created_at"])
    current: set[str] = set()
    for e in labelled:
        if e["actor"] != actor or _ts(e["created_at"]) - start > FIRST_PASS_WINDOW:
            continue
        name = e["label"]
        if e["event"] == "labeled":
            current.add(name)
        else:
            current.discard(name)
    return current & targets


def issue_split(repo_key: str, number: int) -> str:
    """Split from the issue alone, so pulling more or fewer issues never moves one: ~1 in 5 is eval."""
    h = int(hashlib.sha256(f"{repo_key}:{number}".encode()).hexdigest(), 16)
    return "eval" if h % 5 == 0 else "train"


def assign_split(repo_key: str, numbers: list[int], existing: dict[int, str] | None = None) -> dict[int, str]:
    """An issue keeps the split it had in a previous pull; a new issue gets `issue_split`."""
    existing = existing or {}
    return {n: existing.get(n) or issue_split(repo_key, n) for n in numbers}


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ---- network ---------------------------------------------------------------


def fetch_labels(client: httpx.Client, cfg: RepoConfig) -> list[dict]:
    raw = list(_paginate(client, f"/repos/{cfg.full_name}/labels", {"per_page": 100}))
    return filter_labels(cfg, raw)


def fetch_issues(client: httpx.Client, cfg: RepoConfig, n: int, targets: set[str]) -> list[dict]:
    cutoff = datetime.now(UTC) - timedelta(days=MIN_AGE_DAYS)
    out: list[dict] = []
    params = {"state": "all", "per_page": 100, "sort": "created", "direction": "desc"}
    for item in _paginate(client, f"/repos/{cfg.full_name}/issues", params):
        if "pull_request" in item or _ts(item["created_at"]) > cutoff:
            continue
        names = {lab["name"] for lab in item["labels"]}
        if not names & targets:
            continue
        out.append(item)
        if len(out) >= n:
            break
    return out


def fetch_events(client: httpx.Client, cfg: RepoConfig, number: int) -> list[dict]:
    raw = _paginate(client, f"/repos/{cfg.full_name}/issues/{number}/events", {"per_page": 100})
    return [
        {
            "event": e["event"],
            "label": e["label"]["name"],
            "actor": (e.get("actor") or {}).get("login"),
            "created_at": e["created_at"],
        }
        for e in raw
        if e["event"] in ("labeled", "unlabeled")
    ]


def build_dataset(cfg: RepoConfig, n: int, data_dir: Path = DATA_DIR) -> Path:
    out_dir = data_dir / cfg.key
    out_dir.mkdir(parents=True, exist_ok=True)
    with _client() as client:
        labels = fetch_labels(client, cfg)
        targets = {lab["name"] for lab in labels}
        (out_dir / "labels.json").write_text(json.dumps(labels, indent=1))
        issues = fetch_issues(client, cfg, n, targets)
        existing = (
            {r["number"]: r["split"] for r in load_dataset(cfg, data_dir)[1]}
            if (out_dir / "issues.jsonl").exists()
            else {}
        )
        split = assign_split(cfg.key, [i["number"] for i in issues], existing)
        rows = []
        for i in issues:
            events = fetch_events(client, cfg, i["number"])
            final = sorted({lab["name"] for lab in i["labels"]} & targets)
            fp = sorted(first_pass(events, targets))
            rows.append(
                {
                    "number": i["number"],
                    "title": i["title"],
                    "body": clean_body(i["body"], cfg.body_max_chars),
                    "created_at": i["created_at"],
                    "state": i["state"],
                    "url": i["html_url"],
                    "labels": final,
                    "first_pass": fp,
                    "corrected": fp != final and bool(fp),
                    "split": split[i["number"]],
                }
            )
    path = out_dir / "issues.jsonl"
    with path.open("w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    return path


def load_dataset(cfg: RepoConfig, data_dir: Path = DATA_DIR) -> tuple[list[dict], list[dict]]:
    out_dir = data_dir / cfg.key
    labels = json.loads((out_dir / "labels.json").read_text())
    rows = [json.loads(lab) for lab in (out_dir / "issues.jsonl").read_text().splitlines() if lab.strip()]
    return labels, rows
