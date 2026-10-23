"""One classify call, via Mubit or directly to the provider, with everything
needed to detect later that Mubit started serving a different model."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from openai import APIStatusError, OpenAI

from .config import MODEL, MUBIT_BASE_URL, OPENAI_BASE_URL
from .prompt import build_request

ROUTES = ("mubit", "direct")
# No hidden SDK retries: one logged row per request Mubit sees, and latency
# without retry backoff. A failed attempt is logged as an error and excluded.
TIMEOUT_S = 60.0
MAX_RETRIES = 0


@dataclass
class CallResult:
    route: str
    labels: list[str]
    model_asked: str
    model_served: str | None
    request_id: str | None
    latency_ms: int
    prompt_tokens: int | None
    completion_tokens: int | None
    headers: dict = field(default_factory=dict)
    raw: dict | None = None
    error: str | None = None
    unknown_labels: list[str] = field(default_factory=list)


def client_for(route: str) -> OpenAI:
    if route == "mubit":
        key = os.environ.get("MUBIT_API_KEY")
        if not key:
            raise SystemExit("MUBIT_API_KEY is not set")
        return OpenAI(base_url=MUBIT_BASE_URL, api_key=key, timeout=TIMEOUT_S, max_retries=MAX_RETRIES)
    if route == "direct":
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            raise SystemExit("OPENAI_API_KEY is not set (needed for the direct baseline)")
        # Explicit base_url: the SDK would otherwise read OPENAI_BASE_URL, which may point at Mubit.
        return OpenAI(base_url=OPENAI_BASE_URL, api_key=key, timeout=TIMEOUT_S, max_retries=MAX_RETRIES)
    raise SystemExit(f"unknown route {route!r}")


def parse_labels(content: str, allowed: set[str]) -> tuple[list[str], list[str]]:
    """Raises ValueError (json.JSONDecodeError included) on a reply that breaks the schema."""
    data = json.loads(content)
    got = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(got, list) or not all(isinstance(l, str) for l in got):
        raise ValueError(f"'labels' is not a list of strings: {got!r}"[:200])
    kept = sorted({l for l in got if l in allowed})
    unknown = sorted({l for l in got if l not in allowed})
    return kept, unknown


def classify(client: OpenAI, route: str, full_name: str, labels: list[dict],
             issue: dict, guidelines: str, model: str = MODEL) -> CallResult:
    """Always returns a CallResult; any failure is recorded in `error`, never raised."""
    req = build_request(model, full_name, labels, issue, guidelines)
    allowed = {l["name"] for l in labels}
    res = CallResult(route, [], model, None, None, 0, None, None)
    t0 = time.perf_counter()
    try:
        raw = client.chat.completions.with_raw_response.create(**req)
    except APIStatusError as e:  # provider or gateway error reply: keep its headers and body
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        res.headers = dict(e.response.headers)
        res.request_id = e.response.headers.get("x-middleware-request-id")
        res.raw = {"status_code": e.status_code, "body": e.body}
        res.error = f"HTTP {e.status_code}: {e}"[:500]
        return res
    except Exception as e:  # no reply at all (connection error, timeout)
        res.latency_ms = int((time.perf_counter() - t0) * 1000)
        res.error = f"{type(e).__name__}: {e}"[:500]
        return res
    res.latency_ms = int((time.perf_counter() - t0) * 1000)
    res.headers = dict(raw.headers)
    res.request_id = raw.headers.get("x-middleware-request-id")
    try:
        resp = raw.parse()
        res.raw = resp.model_dump()
        res.model_served = resp.model
        if resp.usage:
            res.prompt_tokens = resp.usage.prompt_tokens
            res.completion_tokens = resp.usage.completion_tokens
        if not resp.choices:
            raise ValueError("no choices in reply")
        msg = resp.choices[0].message
        if msg.refusal:
            raise ValueError(f"refusal: {msg.refusal}")
        if not msg.content:
            raise ValueError("empty content")
        res.labels, res.unknown_labels = parse_labels(msg.content, allowed)
    except Exception as e:
        res.labels, res.unknown_labels = [], []
        res.error = f"unparseable reply: {type(e).__name__}: {e}"[:500]
    return res
