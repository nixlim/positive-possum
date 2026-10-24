"""What the step replies mean: parsing with the length limits the schemas cannot express, the
skip rule, and the Edit classification (PIPELINE.md sections 7.4 and 9) rebuilt from logged rows.

Forced review reads only facts measured from the diff, never model output, so an injected edit
summary or a bent extract cannot switch it off. Pure functions."""

from __future__ import annotations

import json

from . import steps
from .page_analytics import CONTROVERSY

SKIP_MAX_DELTA = 20  # |size_delta| below this with no claims: maintenance by rule, not sent
MAX_CLAIM_WORDS = 20
MAX_RATIONALE_WORDS = 25
NET_WORDS_REMOVED = 50
RULE_LABEL = {
    "label": "maintenance",
    "severity": 0,
    "review": False,
    "confidence": None,
    "rationale": f"Under {SKIP_MAX_DELTA} bytes with no claims added or removed.",
}


def _check(obj, schema: dict) -> dict:
    """The reply object restricted to the schema; ValueError when it breaks it. Structured
    output guarantees the schema from the provider, not from whatever Mubit serves instead."""
    if not isinstance(obj, dict) or not set(schema["required"]) <= set(obj):
        raise ValueError(f"reply is not an object with {schema['required']}: {obj!r}"[:200])
    for key, prop in schema["properties"].items():
        v = obj[key]
        ok = {
            "string": isinstance(v, str),
            "integer": isinstance(v, int) and not isinstance(v, bool),
            "boolean": isinstance(v, bool),
            "array": isinstance(v, list) and all(isinstance(x, str) for x in v),
        }[prop["type"]]
        if not ok or ("enum" in prop and v not in prop["enum"]):
            raise ValueError(f"{key}={v!r} breaks the schema"[:200])
    return {k: obj[k] for k in schema["properties"]}


def _words(text: str, n: int) -> str:
    words = text.split()
    return text if len(words) <= n else " ".join(words[:n])


def parse_extract(text: str) -> tuple[list[str], list[str], dict]:
    obj = _check(json.loads(text), steps.EXTRACT_SCHEMA)
    for k in ("claims_added", "claims_removed"):
        obj[k] = [_words(c, MAX_CLAIM_WORDS) for c in obj[k][: steps.MAX_CLAIMS]]
    return [], [], obj


def parse_classify(text: str) -> tuple[list[str], list[str], dict]:
    obj = _check(json.loads(text), steps.CLASSIFY_SCHEMA)
    obj["rationale"] = _words(obj["rationale"], MAX_RATIONALE_WORDS)
    return [obj["label"]], [], obj


def parse_brief(text: str) -> tuple[list[str], list[str], dict]:
    md = text.strip()
    if not md.endswith(steps.CLOSING_LINE):
        md += "\n\n" + steps.CLOSING_LINE
    return [], [], {"brief_md": md}


def extract_of(extract_extra: dict) -> dict:
    return {k: extract_extra[k] for k in steps.EXTRACT_SCHEMA["properties"]}


def skipped(extract_extra: dict) -> bool:
    """The skip rule: a tiny edit whose extract found no claims is not sent to classify."""
    return abs(extract_extra["measured"]["size_delta"]) < SKIP_MAX_DELTA and not (
        extract_extra["claims_added"] or extract_extra["claims_removed"]
    )


def forced_review(measured: dict) -> str | None:
    """The first forced-review rule the measured facts fire, or None."""
    if measured["refs_removed"] >= 1:
        return "refs_removed"
    if measured["words_removed"] - measured["words_added"] >= NET_WORDS_REMOVED:
        return "net_words_removed"
    by_section = measured["words_removed_by_section"]
    if any(n > 0 and CONTROVERSY.search(title) for title, n in by_section.items()):
        return "controversy_words_removed"
    return None


def classification(extract_extra: dict, classify_extra: dict | None) -> dict | None:
    """The Edit classification of an extract row's `extra` and its classify row's `extra`
    (None when classify was not sent): the rule label when the skip rule holds, None when the
    edit is not classified yet. `review` is forced on by the measured facts whatever the model
    said; the model's own value stays in `review_model`."""
    if classify_extra is None:
        if not skipped(extract_extra):
            return None
        out, source = dict(RULE_LABEL), "rule"
    else:
        out, source = {k: classify_extra[k] for k in steps.CLASSIFY_SCHEMA["properties"]}, "model"
    forced = forced_review(extract_extra["measured"])
    return {
        "label": out["label"],
        "severity": out["severity"],
        "review": forced is not None or out["review"],
        "review_model": out["review"],
        "review_forced": forced,
        "confidence": out["confidence"],
        "rationale": out["rationale"],
        "source": source,
    }
