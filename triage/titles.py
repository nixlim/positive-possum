"""The title index (PIPELINE.md section 9, "Titles"): `data/pages/titles.json` maps every title
that resolved to a namespace-0 page, redirect titles included, to its pageid. Snapshots are keyed
by pageid, so every `?title=` lookup goes through it. A CLI ingest and the API may write at once,
so writers serialise on an exclusive lock on `titles.lock` and replace the file atomically."""

from __future__ import annotations

import fcntl
import json
import os
import threading
from pathlib import Path
from urllib.parse import unquote

INDEX, LOCK = "titles.json", "titles.lock"


def normalise(title: str) -> str:
    """enwiki's form of a title: percent-decoded (a /wiki/ path), underscores as spaces,
    whitespace trimmed and collapsed, the first character upper-cased and the rest kept."""
    t = " ".join(unquote(title).replace("_", " ").split())
    return t[:1].upper() + t[1:]


def load(pages_dir: Path) -> dict[str, int]:
    path = pages_dir / INDEX
    return json.loads(path.read_text()) if path.exists() else {}


def lookup(pages_dir: Path, title: str) -> int | None:
    return load(pages_dir).get(normalise(title))


def record(pages_dir: Path, entries: dict[str, int]) -> None:
    """Merges `entries` into the index: under the lock, re-read, merge, write a temporary file
    and rename it over the index, so no concurrent writer's entry is lost."""
    pages_dir.mkdir(parents=True, exist_ok=True)
    with open(pages_dir / LOCK, "a") as lock:  # closing it releases the lock
        fcntl.flock(lock, fcntl.LOCK_EX)
        index = load(pages_dir) | {normalise(t): pageid for t, pageid in entries.items()}
        path = pages_dir / INDEX
        tmp = path.with_name(f"{INDEX}.{os.getpid()}.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(index, sort_keys=True, ensure_ascii=False))
        tmp.replace(path)
