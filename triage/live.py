"""Live patrol helpers: turn Wikimedia recent-change events into dataset-shaped
items, and summarise the call log for the patrol desk. Pure functions except
`iter_events` and `fetch_item`, which talk to Wikimedia."""

from __future__ import annotations

import ipaddress
import json
import re
import statistics
from collections.abc import Iterator
from datetime import UTC, datetime

import httpx

from .config import PRICE_USD_PER_MTOK, is_provider
from .wiki import edit_body, fetch_contents

STREAM_URL = "https://stream.wikimedia.org/v2/stream/recentchange"
# The feed carries every wiki's edits, many per second: a minute of silence is a stalled
# (half-open) connection, so raise and let the reader reconnect.
STREAM_TIMEOUT = httpx.Timeout(10, read=60)
# Edits that undo something are trivially "keep" and their summaries give the game away.
REVERTING_SUMMARY = re.compile(r"^\s*(reverted|revert\b|rv\b|undid revision|undo\b)", re.I)


def is_ip(user: str) -> bool:
    try:
        ipaddress.ip_address(user)
    except ValueError:
        return False
    return True


def wants(event: dict, host: str) -> bool:
    """A human content edit on `host` that is not itself a revert."""
    return (
        event.get("type") == "edit"
        and event.get("server_name") == host
        and event.get("namespace") == 0
        and not event.get("bot")
        and bool((event.get("revision") or {}).get("old"))
        and not REVERTING_SUMMARY.match(event.get("comment") or "")
    )


def rc_from_event(event: dict) -> dict:
    """The recentchanges-API shape that `wiki.edit_body` reads."""
    user = event.get("user") or ""
    anon = is_ip(user)
    ts = datetime.fromtimestamp(event["timestamp"], tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "revid": event["revision"]["new"],
        "old_revid": event["revision"]["old"],
        "title": event["title"],
        "comment": event.get("comment") or "",
        "timestamp": ts,
        "user": user,
        "userid": 0 if anon else 1,
        "anon": anon,
        "oldlen": (event.get("length") or {}).get("old", 0),
        "newlen": (event.get("length") or {}).get("new", 0),
    }


def item_from_event(event: dict, old: str, new: str, max_chars: int) -> dict:
    """A dataset-shaped row (what `classify` and the guidelines expect) plus display fields."""
    rc = rc_from_event(event)
    return {
        "number": rc["revid"],
        "title": rc["title"],
        "body": edit_body(rc, old, new, max_chars),
        "url": f"{event['server_url']}/w/index.php?diff={rc['revid']}",
        "user": rc["user"],
        "anon": rc["anon"],
        "comment": rc["comment"],
        "created_at": rc["timestamp"],
    }


def fetch_item(client: httpx.Client, event: dict, max_chars: int) -> dict | None:
    """None when either revision is unavailable (suppressed, deleted, or not replicated yet)."""
    rev = event["revision"]
    texts = fetch_contents(client, event["server_name"], [rev["old"], rev["new"]])
    if rev["old"] not in texts or rev["new"] not in texts:
        return None
    return item_from_event(event, texts[rev["old"]], texts[rev["new"]], max_chars)


def iter_events(client: httpx.Client, host: str) -> Iterator[dict]:
    """Wanted edits from the Wikimedia EventStreams SSE feed, until the connection drops."""
    with client.stream(
        "GET", STREAM_URL, headers={"Accept": "text/event-stream"}, timeout=STREAM_TIMEOUT
    ) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            try:
                event = json.loads(line[5:])
            except json.JSONDecodeError:
                continue
            if wants(event, host):
                yield event


def cost_usd(prompt_tokens: int | None, completion_tokens: int | None) -> float:
    return (
        (prompt_tokens or 0) * PRICE_USD_PER_MTOK["input"]
        + (completion_tokens or 0) * PRICE_USD_PER_MTOK["output"]
    ) / 1e6


def is_owned(served: str | None, model: str) -> bool:
    """A reply Mubit's own model answered: a served model is known and is not the provider's.
    An unknown served model is not counted as owned; that would claim a switch we cannot see."""
    return bool(served) and not is_provider(served, model)


def _served_stats(rows: list[dict], model: str) -> dict:
    return {
        "owned": is_owned(rows[0]["model_served"], model),
        "n": len(rows),
        "p50_ms": int(statistics.median(r["latency_ms"] for r in rows)),
        "input_tokens": sum(r["prompt_tokens"] or 0 for r in rows),
        "output_tokens": sum(r["completion_tokens"] or 0 for r in rows),
    }


def summarize(calls: list[dict], model: str, now: datetime | None = None) -> dict:
    """Patrol-desk numbers from `Store.calls_since` rows. Everything here is measured from
    our own log; nothing is read from Mubit, which exposes no metrics."""
    now = now or datetime.now(UTC)
    answered = [c for c in calls if not c["error"]]
    by_served: dict[str, list[dict]] = {}
    for c in answered:
        by_served.setdefault(c["model_served"] or "?", []).append(c)
    owned = [c for c in answered if is_owned(c["model_served"], model)]
    frontier_cost = sum(cost_usd(c["prompt_tokens"], c["completion_tokens"]) for c in answered)
    per_day = None
    if answered:
        first = datetime.fromisoformat(answered[0]["ts"])
        hours = max((now - first).total_seconds() / 3600, 1 / 60)
        per_day = frontier_cost * 24 / hours
    return {
        "calls": len(calls),
        "answered": len(answered),
        "errors": len(calls) - len(answered),
        "owned_share": (len(owned) / len(answered)) if answered else 0.0,
        "by_served": {m: _served_stats(rows, model) for m, rows in by_served.items()},
        "frontier_cost_usd": round(frontier_cost, 4),
        "frontier_cost_per_day_usd": round(per_day, 2) if per_day is not None else None,
        "owned_cost_per_day_usd": None,  # Mubit publishes no adapter price
        "corrections": sum(c["kind"] == "correct" for c in calls),
        "model": model,
    }
