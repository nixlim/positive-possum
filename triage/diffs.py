"""One revision against its parent: the trimmed diff the extract step reads, and the facts
measured in code (words, citations, sections) so the model is never asked to count."""

from __future__ import annotations

import re
from collections import Counter
from difflib import SequenceMatcher

from .wiki import diff_text

DIFF_MAX_CHARS = 6000
LEAD = "(lead)"
WORD = re.compile(r"\w+")
HEADING = re.compile(r"^(=+)\s*(.+?)\s*\1\s*$")
# One citation per `<ref` opening: `<ref>…</ref>`, `<ref name=…>…</ref>` and `<ref name=… />`.
# `\b` keeps `<references />` out.
REF = re.compile(r"<ref\b[^>]*?/>|<ref\b[^>]*>.*?(?:</ref\s*>|$)", re.IGNORECASE | re.DOTALL)


def _changed_words(old_lines: list[str], new_lines: list[str]) -> tuple[int, list[int]]:
    """Words added, and for each word removed the index of its line in `old_lines`."""
    a = [(w, i) for i, line in enumerate(old_lines) for w in WORD.findall(line)]
    b = [w for line in new_lines for w in WORD.findall(line)]
    added, removed = 0, []
    for tag, i1, i2, j1, j2 in SequenceMatcher(None, [w for w, _ in a], b, autojunk=False).get_opcodes():
        if tag != "equal":
            removed += [a[k][1] for k in range(i1, i2)]
            added += j2 - j1
    return added, removed


def _refs(old: str, new: str) -> tuple[int, int]:
    """Citations added and removed: a moved citation counts as neither, an edited one as both."""
    a, b = Counter(REF.findall(old)), Counter(REF.findall(new))
    return (b - a).total(), (a - b).total()


def _section_at(lines: list[str]) -> list[str]:
    """The section each line belongs to (a heading line belongs to its own section)."""
    out, current = [], LEAD
    for line in lines:
        m = HEADING.match(line)
        if m:
            current = m.group(2)
        out.append(current)
    return out


def measure(old: str, new: str) -> dict:
    """words_added/removed (word tokens of the wikitext, markup included), refs_added/removed,
    the sections touched in page order of first appearance, and the removed words per old-side
    section (what the forced-review rule reads)."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    in_old, in_new = _section_at(old_lines), _section_at(new_lines)
    opcodes = SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_opcodes()
    words_added, by_section, touched = 0, Counter(), {}
    for tag, i1, i2, j1, j2 in opcodes:
        if tag == "equal":
            continue
        for name in in_old[i1:i2] + in_new[j1:j2]:
            touched.setdefault(name, None)
        added, removed = _changed_words(old_lines[i1:i2], new_lines[j1:j2])
        words_added += added
        by_section.update(in_old[i1 + k] for k in removed)
    refs_added, refs_removed = _refs(old, new)
    return {
        "words_added": words_added,
        "words_removed": by_section.total(),
        "refs_added": refs_added,
        "refs_removed": refs_removed,
        "sections": list(touched),
        "words_removed_by_section": dict(by_section),
    }


def edit_diff(old: str, new: str) -> tuple[str, dict]:
    """(diff for the prompt, at most DIFF_MAX_CHARS plus the truncation mark; measured facts)."""
    return diff_text(old, new, DIFF_MAX_CHARS), measure(old, new)
