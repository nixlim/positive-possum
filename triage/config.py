"""Repo definitions, label curation rules and environment."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def load_dotenv(path: Path = Path(".env")) -> None:
    """Minimal .env loader: KEY=VALUE lines, never overrides a set variable."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        os.environ.setdefault(key, value)


@dataclass(frozen=True)
class RepoConfig:
    key: str
    full_name: str
    include_prefixes: tuple[str, ...]
    include_exact: tuple[str, ...]
    exclude_exact: tuple[str, ...] = ()
    body_max_chars: int = 2500

    def is_target(self, label: str) -> bool:
        if label in self.exclude_exact:
            return False
        if label in self.include_exact:
            return True
        return any(label.startswith(p) for p in self.include_prefixes)


REPOS: dict[str, RepoConfig] = {
    "godot": RepoConfig(
        key="godot",
        full_name="godotengine/godot",
        include_prefixes=("topic:", "platform:"),
        include_exact=(
            "bug", "enhancement", "regression", "crash", "performance",
            "usability", "documentation", "discussion",
        ),
    ),
    "flutter": RepoConfig(
        key="flutter",
        full_name="flutter/flutter",
        include_prefixes=("team-", "platform-", "c: ", "a: "),
        include_exact=("engine", "framework", "tool", "package"),
        exclude_exact=("team-infra",),
        body_max_chars=3000,
    ),
}

MUBIT_BASE_URL = "https://api.mubit.ai/openai/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"
load_dotenv()  # before reading TRIAGE_* below, so they can be set in .env
MODEL = os.environ.get("TRIAGE_MODEL", "gpt-6-luna")
DATA_DIR = Path(os.environ.get("TRIAGE_DATA", "data"))
MIN_AGE_DAYS = 7  # labels have usually settled by then


def repo(key: str) -> RepoConfig:
    try:
        return REPOS[key]
    except KeyError:
        raise SystemExit(f"unknown repo {key!r}; choose from {', '.join(REPOS)}")
