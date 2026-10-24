"""Features of one Wikipedia edit for the local (no-LLM) edit classifier. Pure functions.

Input is what is known when the edit is saved: the recent-changes row (editor, sizes, flags,
save-time tags, summary), the editor's account (edit count, registration) and the diff. Never
the `mw-reverted` tag, the reverting edit, or anything else that happens after the save."""

from __future__ import annotations

import math
import re
from datetime import datetime
from itertools import groupby

from .wiki import REVERTED_TAG, editor_kind

TEXT_CAP = 20_000  # characters of added or removed text kept per edit

# Save-time tags worth a feature: the editing interface and AbuseFilter's own flags. All are
# applied by MediaWiki when the edit is saved; `mw-reverted` is applied later and is the label.
SAVE_TIME_TAGS = (
    "mobile edit",
    "mobile web edit",
    "mobile app edit",
    "visualeditor",
    "visualeditor-wikitext",
    "wikieditor",
    "possible vandalism",
    "repeating characters",
    "references removed",
    "possible BLP issue or vandalism",
    "mw-replace",
    "mw-blank",
    "blanking",
    "disambiguation links added",
    "mw-new-redirect",
    "mw-removed-redirect",
    "advanced mobile edit",
    "AWB",
    "editcheck-references",
    "editcheck-newcontent",
)
assert REVERTED_TAG not in SAVE_TIME_TAGS

# Obscenities, insults and chat filler: a small lexicon, counted on whole words.
LEXICON = re.compile(
    r"\b(fuck\w*|shit\w*|bitch\w*|cunts?|dick(head)?s?|cocks?|pussy|ass(hole)?s?|arsehole\w*|"
    r"fag(got)?s?|nigg\w*|retard\w*|whores?|sluts?|bastards?|wank\w*|poo+p?|butts?|boobs?|penis|"
    r"stupid|idiots?|dumb|sucks?|losers?|gay|lo+l|lmao|ha(ha)+|he(he)+|yo+|sup|hi+)\b",
    re.I,
)
SECTION_ONLY = re.compile(r"^\s*/\*.*?\*/\s*$")
SECTION_PREFIX = re.compile(r"^\s*/\*.*?\*/\s*")
COUNTED = {
    "refs": re.compile(r"<ref\b", re.I),
    "templates": re.compile(r"\{\{"),
    "wikilinks": re.compile(r"\[\[(?!\s*(category|file|image):)", re.I),
    "urls": re.compile(r"https?://", re.I),
    "categories": re.compile(r"\[\[\s*category:", re.I),
    "images": re.compile(r"\[\[\s*(file|image):|\.(jpe?g|png|gif|svg|webp)\b", re.I),
}


def parse_compare(data: dict) -> dict:
    """Added and removed text from a REST `revision/{old}/compare/{new}` reply. Line types:
    0 context, 1 line added, 2 line removed, 3 line changed in place, 4/5 paragraph moved
    from/to. For 3 and 5 the line text interleaves old and new, and `highlightRanges` mark
    inserted (type 0) and deleted (type 1) spans as UTF-8 byte offsets."""
    added: list[str] = []
    removed: list[str] = []
    for line in data.get("diff", []):
        kind, text = line.get("type"), line.get("text") or ""
        if kind == 1:
            added.append(text)
        elif kind == 2:
            removed.append(text)
        elif kind in (3, 5):
            raw = text.encode()
            for h in line.get("highlightRanges") or []:
                seg = raw[h["start"] : h["start"] + h["length"]].decode("utf-8", "ignore")
                (added if h["type"] == 0 else removed).append(seg)
    return {"added": "\n".join(added)[:TEXT_CAP], "removed": "\n".join(removed)[:TEXT_CAP]}


def summary_text(comment: str | None) -> str:
    """What the editor typed, without the automatic `/* Section */` prefix."""
    return SECTION_PREFIX.sub("", comment or "").strip()


def longest_run(text: str) -> int:
    return max((sum(1 for _ in g) for _, g in groupby(text)), default=0)


def _letters(text: str) -> list[str]:
    return [c for c in text if c.isalpha()]


def is_latin(ch: str) -> bool:
    o = ord(ch)
    return o <= 0x24F or 0x1E00 <= o <= 0x1EFF


def text_stats(added: str) -> dict[str, float]:
    letters = _letters(added)
    n = len(letters)
    return {
        "upper_ratio": sum(c.isupper() for c in letters) / n if n else 0.0,
        "longest_run": float(longest_run(added)),
        "exclamations": float(added.count("!")),
        "lexicon_hits": float(len(LEXICON.findall(added))),
        "nonlatin_share": sum(not is_latin(c) for c in letters) / n if n else 0.0,
    }


def markup_counts(added: str, removed: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for name, rx in COUNTED.items():
        out[f"{name}_added"] = float(len(rx.findall(added)))
        out[f"{name}_removed"] = float(len(rx.findall(removed)))
    return out


def change_flags(added: str, removed: str) -> dict[str, float]:
    def squash(s: str) -> str:
        return re.sub(r"\s+", "", s)

    changed = added + removed
    return {
        "whitespace_only": float(bool(changed) and squash(added) == squash(removed)),
        "digits_only": float(
            any(c.isdigit() for c in changed) and not re.search(r"[^\d\s.,%\-–/:]", changed)
        ),
    }


def summary_flags(comment: str | None) -> dict[str, float]:
    typed = summary_text(comment)
    return {
        "summary_present": float(bool(typed)),
        "summary_section_only": float(bool(SECTION_ONLY.match(comment or ""))),
        "summary_len": float(len(typed)),
    }


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def editor_features(rc: dict, user: dict | None) -> dict[str, float]:
    kind = editor_kind(rc)
    count = (user or {}).get("editcount")
    reg = (user or {}).get("registration")
    age = (_ts(rc["timestamp"]) - _ts(reg)).total_seconds() / 86400 if reg else None
    return {
        "editor_temporary": float(kind == "temporary"),
        "editor_anonymous": float(kind == "anonymous"),
        "editor_registered": float(kind == "registered"),
        "log_editcount": math.log1p(count) if count is not None else math.nan,
        "log_account_age_days": math.log1p(max(age, 0.0)) if age is not None else math.nan,
        "minor": float(bool(rc.get("minor"))),
    }


def tag_flags(tags: list[str]) -> dict[str, float]:
    present = set(tags or [])
    return {f"tag_{t.replace(' ', '_')}": float(t in present) for t in SAVE_TIME_TAGS}


def edit_features(rc: dict, diff: dict, user: dict | None, with_summary: bool = True) -> dict[str, float]:
    """All numeric features of one edit. `with_summary=False` drops everything read from the
    edit summary (for the declared-intent model, whose labels come from the summary)."""
    added, removed = diff.get("added") or "", diff.get("removed") or ""
    delta = (rc.get("newlen") or 0) - (rc.get("oldlen") or 0)
    out = {
        **editor_features(rc, user),
        "size_delta": float(delta),
        "abs_delta": float(abs(delta)),
        "log_page_size": math.log1p(rc.get("oldlen") or 0),
        "added_chars": float(len(added)),
        "removed_chars": float(len(removed)),
        "added_words": float(len(added.split())),
        **text_stats(added),
        **markup_counts(added, removed),
        **change_flags(added, removed),
        **tag_flags(rc.get("tags") or []),
    }
    if with_summary:
        out.update(summary_flags(rc.get("comment")))
    return out
