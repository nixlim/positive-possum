"""SQLite log of every call and every eval run. This is the evidence trail."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .config import DATA_DIR
from .llm import CallResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,          -- eval | replay | correct
  route TEXT NOT NULL,         -- always mubit: there is no other egress
  repo TEXT NOT NULL,
  issue INTEGER NOT NULL,
  model_asked TEXT,
  model_served TEXT,
  request_id TEXT,
  latency_ms INTEGER,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  labels TEXT NOT NULL,        -- JSON list
  truth TEXT,                  -- JSON list, when known
  error TEXT,
  headers TEXT,
  raw TEXT
);
CREATE INDEX IF NOT EXISTS calls_run ON calls(run_id);
CREATE INDEX IF NOT EXISTS calls_repo_route ON calls(repo, route, ts);
CREATE TABLE IF NOT EXISTS runs (
  run_id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  kind TEXT NOT NULL,
  route TEXT NOT NULL,
  repo TEXT NOT NULL,
  n INTEGER NOT NULL,
  errors INTEGER NOT NULL,
  metrics TEXT NOT NULL        -- JSON
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | None = None):
        path = path or DATA_DIR / "calls.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)

    def log_call(self, run_id: str, kind: str, repo: str, issue: int, r: CallResult,
                 truth: list[str] | None = None) -> None:
        self.db.execute(
            "INSERT INTO calls (ts, run_id, kind, route, repo, issue, model_asked, model_served, request_id,"
            " latency_ms, prompt_tokens, completion_tokens, labels, truth, error, headers, raw)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (now(), run_id, kind, r.route, repo, issue, r.model_asked, r.model_served, r.request_id,
             r.latency_ms, r.prompt_tokens, r.completion_tokens, json.dumps(r.labels),
             json.dumps(truth) if truth is not None else None, r.error,
             json.dumps(r.headers), json.dumps(r.raw) if r.raw is not None else None),
        )
        self.db.commit()

    def log_run(self, run_id: str, kind: str, route: str, repo: str, n: int, errors: int, metrics: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?,?,?,?)",
            (run_id, now(), kind, route, repo, n, errors, json.dumps(metrics)),
        )
        self.db.commit()

    def predictions(self, run_id: str) -> dict[int, list[str]]:
        rows = self.db.execute("SELECT issue, labels FROM calls WHERE run_id=? AND error IS NULL", (run_id,))
        return {issue: json.loads(labels) for issue, labels in rows}

    def latest_prediction(self, repo: str, issue: int) -> list[str] | None:
        """Latest Mubit eval/replay answer: not a `correct` re-send (its guidelines held the answer)."""
        row = self.db.execute(
            "SELECT labels FROM calls WHERE repo=? AND issue=? AND error IS NULL AND route='mubit'"
            " AND kind IN ('eval','replay') ORDER BY ts DESC, id DESC LIMIT 1",
            (repo, issue),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def runs(self, repo: str | None = None) -> list[dict]:
        q = "SELECT run_id, ts, kind, route, repo, n, errors, metrics FROM runs"
        args: tuple = ()
        if repo:
            q += " WHERE repo=?"
            args = (repo,)
        q += " ORDER BY ts"
        # `calls` counts every call in the run; `n` is the scored (non-errored) count.
        return [
            {"run_id": r[0], "ts": r[1], "kind": r[2], "route": r[3], "repo": r[4],
             **json.loads(r[7]), "n": r[5] - r[6], "calls": r[5], "errors": r[6]}
            for r in self.db.execute(q, args)
        ]

    def served_models(self, run_id: str) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT COALESCE(model_served,'?'), COUNT(*) FROM calls WHERE run_id=? GROUP BY 1", (run_id,)
        )
        return dict(rows)
