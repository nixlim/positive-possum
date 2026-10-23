"""Per-repo maintainer guidelines: the only lever that changes recorded replies
without forking the Mubit task. One line per correction, appended forever."""
from __future__ import annotations

import re
from pathlib import Path

from .config import DATA_DIR

_ISSUE_RE = re.compile(r"- \(#(\d+)\) ")


def path(repo_key: str, data_dir: Path = DATA_DIR) -> Path:
    return data_dir / "guidelines" / f"{repo_key}.md"


def load(repo_key: str, eval_issues: set[int] = frozenset(), data_dir: Path = DATA_DIR) -> str:
    """Guidelines for a prompt. Lines from corrections of issues now in `eval_issues` are
    dropped: they hold that issue's ground truth, and the split can change on a re-pull."""
    p = path(repo_key, data_dir)
    if not p.exists():
        return ""
    lines = p.read_text().splitlines(keepends=True)
    return "".join(l for l in lines if (m := _ISSUE_RE.match(l)) is None or int(m[1]) not in eval_issues)


def guideline_line(issue: dict, predicted: list[str], final: list[str]) -> str:
    added = sorted(set(final) - set(predicted))
    removed = sorted(set(predicted) - set(final))
    parts = []
    if added:
        parts.append("add " + ", ".join(added))
    if removed:
        parts.append("drop " + ", ".join(removed))
    change = "; ".join(parts) or "keep labels as predicted"
    return (f"- (#{issue['number']}) For items like \"{issue['title']}\": {change} "
            f"(final labels: {', '.join(final) or 'none'}).")


def add_correction(repo_key: str, issue: dict, predicted: list[str], final: list[str],
                   data_dir: Path = DATA_DIR) -> str:
    line = guideline_line(issue, predicted, final)
    p = path(repo_key, data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(line + "\n")
    return line
