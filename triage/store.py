"""SQLite log of every call and every eval run. This is the evidence trail."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .config import DATA_DIR
from .llm import CallResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS calls (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  run_id TEXT NOT NULL,
  kind TEXT NOT NULL,          -- eval | replay | correct | live
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
  raw TEXT,
  extra TEXT                   -- JSON object: reply fields besides labels
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
CREATE TABLE IF NOT EXISTS outcomes (     -- what Wikipedia did after a live verdict
  repo TEXT NOT NULL,
  issue INTEGER NOT NULL,
  verdict_ts TEXT,
  verdict TEXT,
  truth TEXT NOT NULL,         -- keep | revert | gone (revision no longer returned)
  reverted_at TEXT,
  reverted_by TEXT,
  revert_revid INTEGER,
  lead_s INTEGER,              -- reverted_at minus verdict_ts, when both are known
  checked_at TEXT NOT NULL,
  PRIMARY KEY (repo, issue)
);
CREATE TABLE IF NOT EXISTS rule_labels (  -- edits the skip rule labelled; no call was made
  revid INTEGER PRIMARY KEY,
  label TEXT NOT NULL,
  measured TEXT NOT NULL,      -- JSON: the measured facts the rule read
  extract_call_id INTEGER,
  ts TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS unavailable_diffs (  -- revids a successful wikitext reply left out
  revid INTEGER PRIMARY KEY,
  missing TEXT NOT NULL,       -- revision | parent
  ts TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path | None = None):
        path = path or DATA_DIR / "calls.sqlite"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.executescript(SCHEMA)
        # Logs written before the `extra` column existed.
        if "extra" not in {r[1] for r in self.db.execute("PRAGMA table_info(calls)")}:
            self.db.execute("ALTER TABLE calls ADD COLUMN extra TEXT")
            self.db.commit()

    def log_call(
        self, run_id: str, kind: str, repo: str, issue: int, r: CallResult, truth: list[str] | None = None
    ) -> int:
        """Logs one call; returns its row id."""
        cur = self.db.execute(
            "INSERT INTO calls (ts, run_id, kind, route, repo, issue, model_asked, model_served, request_id,"
            " latency_ms, prompt_tokens, completion_tokens, labels, truth, error, headers, raw, extra)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                now(),
                run_id,
                kind,
                r.route,
                repo,
                issue,
                r.model_asked,
                r.model_served,
                r.request_id,
                r.latency_ms,
                r.prompt_tokens,
                r.completion_tokens,
                json.dumps(r.labels),
                json.dumps(truth) if truth is not None else None,
                r.error,
                json.dumps(r.headers),
                json.dumps(r.raw) if r.raw is not None else None,
                json.dumps(r.extra) if r.extra else None,
            ),
        )
        self.db.commit()
        return cur.lastrowid

    def calls_since(self, repo: str, kinds: tuple[str, ...], since_ts: str) -> list[dict]:
        """Answered and errored calls of the given kinds since an ISO timestamp, oldest first."""
        marks = ",".join("?" * len(kinds))
        rows = self.db.execute(
            f"SELECT ts, kind, issue, model_asked, model_served, latency_ms, prompt_tokens,"
            f" completion_tokens, labels, truth, error, extra, request_id FROM calls"
            f" WHERE repo=? AND kind IN ({marks}) AND ts>=? ORDER BY ts, id",
            (repo, *kinds, since_ts),
        )
        return [
            {
                "ts": r[0],
                "kind": r[1],
                "issue": r[2],
                "model_asked": r[3],
                "model_served": r[4],
                "latency_ms": r[5],
                "prompt_tokens": r[6],
                "completion_tokens": r[7],
                "labels": json.loads(r[8]),
                "truth": json.loads(r[9]) if r[9] else None,
                "error": r[10],
                "extra": json.loads(r[11]) if r[11] else {},
                "request_id": r[12],
            }
            for r in rows
        ]

    def answered(self, repo: str, skip_kinds: tuple[str, ...] = (), first: bool = False) -> dict[int, dict]:
        """Latest (or `first`) non-error call per issue of a repo, calls of `skip_kinds` left out:
        {issue: {id, model_served, labels, extra}}. The pipeline resumes from it and chains
        classify to its extract row."""
        marks = ",".join("?" * len(skip_kinds))
        rows = self.db.execute(
            "SELECT id, issue, model_served, labels, extra FROM calls WHERE repo=? AND error IS NULL"
            f" AND kind NOT IN ({marks}) ORDER BY id {'DESC' if first else 'ASC'}",
            (repo, *skip_kinds),
        )
        return {
            r[1]: {
                "id": r[0],
                "model_served": r[2],
                "labels": json.loads(r[3]),
                "extra": json.loads(r[4]) if r[4] else {},
            }
            for r in rows
        }

    def log_run(
        self, run_id: str, kind: str, route: str, repo: str, n: int, errors: int, metrics: dict
    ) -> None:
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
            {
                "run_id": r[0],
                "ts": r[1],
                "kind": r[2],
                "route": r[3],
                "repo": r[4],
                **json.loads(r[7]),
                "n": r[5] - r[6],
                "calls": r[5],
                "errors": r[6],
            }
            for r in self.db.execute(q, args)
        ]

    def served_models(self, run_id: str) -> dict[str, int]:
        rows = self.db.execute(
            "SELECT COALESCE(model_served,'?'), COUNT(*) FROM calls WHERE run_id=? GROUP BY 1", (run_id,)
        )
        return dict(rows)

    # ---- outcomes (live verdicts reconciled with Wikipedia) -------------------

    OUTCOME_COLS = (
        "issue",
        "verdict_ts",
        "verdict",
        "truth",
        "reverted_at",
        "reverted_by",
        "revert_revid",
        "lead_s",
    )

    def pending_outcomes(self, repo: str) -> list[dict]:
        """Answered live calls (first answer per edit) that have no outcome yet, oldest first."""
        rows = self.db.execute(
            "SELECT MIN(ts), issue, labels FROM calls WHERE repo=? AND kind='live' AND error IS NULL"
            " AND issue NOT IN (SELECT issue FROM outcomes WHERE repo=?) GROUP BY issue ORDER BY 1",
            (repo, repo),
        )
        return [{"ts": r[0], "issue": r[1], "labels": json.loads(r[2])} for r in rows]

    def log_outcome(self, repo: str, o: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO outcomes (repo, issue, verdict_ts, verdict, truth, reverted_at,"
            " reverted_by, revert_revid, lead_s, checked_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (repo, *(o.get(c) for c in self.OUTCOME_COLS), now()),
        )
        self.db.commit()

    def outcomes(self, repo: str) -> list[dict]:
        cols = ", ".join(self.OUTCOME_COLS)
        rows = self.db.execute(
            f"SELECT {cols}, checked_at FROM outcomes WHERE repo=? ORDER BY verdict_ts", (repo,)
        )
        return [dict(zip((*self.OUTCOME_COLS, "checked_at"), r, strict=True)) for r in rows]

    # ---- edit state besides the calls (PIPELINE.md section 7.4) ---------------

    def errors(self, repos: tuple[str, ...], skip_kinds: tuple[str, ...] = ()) -> list[dict]:
        """Errored calls of the repos, calls of `skip_kinds` left out, oldest first:
        [{id, run_id, repo, issue, error, extra}]."""
        rmarks, kmarks = ",".join("?" * len(repos)), ",".join("?" * len(skip_kinds))
        rows = self.db.execute(
            f"SELECT id, run_id, repo, issue, error, extra FROM calls WHERE repo IN ({rmarks})"
            f" AND error IS NOT NULL AND kind NOT IN ({kmarks}) ORDER BY id",
            (*repos, *skip_kinds),
        )
        keys = ("id", "run_id", "repo", "issue", "error")
        return [
            dict(zip(keys, r[:5], strict=True)) | {"extra": json.loads(r[5]) if r[5] else {}} for r in rows
        ]

    def log_rule_label(self, revid: int, label: str, measured: dict, extract_call_id: int) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO rule_labels VALUES (?,?,?,?,?)",
            (revid, label, json.dumps(measured), extract_call_id, now()),
        )
        self.db.commit()

    def rule_labels(self) -> dict[int, dict]:
        rows = self.db.execute("SELECT revid, label, measured, extract_call_id, ts FROM rule_labels")
        return {
            r[0]: {"label": r[1], "measured": json.loads(r[2]), "extract_call_id": r[3], "ts": r[4]}
            for r in rows
        }

    def log_unavailable(self, revid: int, missing: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO unavailable_diffs VALUES (?,?,?)", (revid, missing, now()))
        self.db.commit()

    def unavailable_diffs(self) -> dict[int, str]:
        return dict(self.db.execute("SELECT revid, missing FROM unavailable_diffs"))
