"""Repo definitions, label curation rules and environment."""

from __future__ import annotations

import os
import re
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
    """A Mubit task source. kind "github": label issues of repo `full_name`;
    kind "wiki": predict reverts on the MediaWiki host `full_name`."""

    key: str
    full_name: str
    include_prefixes: tuple[str, ...] = ()
    include_exact: tuple[str, ...] = ()
    exclude_exact: tuple[str, ...] = ()
    body_max_chars: int = 2500
    kind: str = "github"

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
            "bug",
            "enhancement",
            "regression",
            "crash",
            "performance",
            "usability",
            "documentation",
            "discussion",
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
    "enwiki": RepoConfig(key="enwiki", full_name="en.wikipedia.org", kind="wiki", body_max_chars=3000),
}

# Anthropic list price for claude-sonnet-5-5 (USD per million tokens, October 2026). Only used
# to project what the frontier model costs; Mubit does not publish a price for its adapter.
PRICE_USD_PER_MTOK = {"input": 2.0, "output": 10.0}

# Mubit is the only model egress (hackathon rule). Anthropic SDK base: Mubit adds /v1 itself.
MUBIT_ANTHROPIC_BASE_URL = "https://api.mubit.ai/anthropic"
load_dotenv()  # before reading TRIAGE_* below, so they can be set in .env
MODEL = os.environ.get("TRIAGE_MODEL", "claude-sonnet-5-5")
DATA_DIR = Path(os.environ.get("TRIAGE_DATA", "data"))
MIN_AGE_DAYS = 7  # labels have usually settled by then


def is_provider(served: str | None, asked: str) -> bool:
    """True when the served model string is the requested provider model (a date suffix
    allowed), i.e. Mubit forwarded to the provider and no adapter answered."""
    return bool(served) and re.fullmatch(re.escape(asked) + r"(-\d{8})?", served) is not None


def repo(key: str) -> RepoConfig:
    try:
        return REPOS[key]
    except KeyError:
        raise SystemExit(f"unknown repo {key!r}; choose from {', '.join(REPOS)}") from None
