"""One classify call through Mubit, with everything needed to detect later
that Mubit started serving a different model.

Mubit is the only egress (hackathon rule: every LLM call goes through it). The
Anthropic SDK is pointed at Mubit's Anthropic base URL with the Mubit key as
its only credential."""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

from anthropic import Anthropic, APIStatusError

from .config import MODEL, MUBIT_ANTHROPIC_BASE_URL
from .prompt import build_request

ROUTE = "mubit"
# No hidden SDK retries: one logged row per request Mubit sees, and latency
# without retry backoff. A failed attempt is logged as an error and excluded.
TIMEOUT_S = 60.0
MAX_RETRIES = 0


@dataclass
class CallResult:
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
    route: str = ROUTE


def client() -> Anthropic:
    key = os.environ.get("MUBIT_API_KEY")
    if not key:
        raise SystemExit("MUBIT_API_KEY is not set")
    # The SDK merges ANTHROPIC_CUSTOM_HEADERS into every request; it could carry a second
    # credential (Mubit answers 401) or anything else, so refuse rather than send it.
    if os.environ.get("ANTHROPIC_CUSTOM_HEADERS") is not None:
        raise SystemExit("ANTHROPIC_CUSTOM_HEADERS is set; unset it: only the Mubit key may be sent")
    # Explicit base_url and key: the SDK would otherwise read ANTHROPIC_BASE_URL and
    # ANTHROPIC_API_KEY. The locked SDK reads no credential from the environment once
    # api_key is given; clearing auth_token after guards older SDKs that still fell back
    # to ANTHROPIC_AUTH_TOKEN and would send it as a second credential.
    c = Anthropic(base_url=MUBIT_ANTHROPIC_BASE_URL, api_key=key, auth_token=None,
                  timeout=TIMEOUT_S, max_retries=MAX_RETRIES)
    c.auth_token = None
    return c


def parse_labels(content: str, allowed: set[str]) -> tuple[list[str], list[str]]:
    """Raises ValueError (json.JSONDecodeError included) on a reply that breaks the schema."""
    data = json.loads(content)
    got = data.get("labels") if isinstance(data, dict) else None
    if not isinstance(got, list) or not all(isinstance(l, str) for l in got):
        raise ValueError(f"'labels' is not a list of strings: {got!r}"[:200])
    kept = sorted({l for l in got if l in allowed})
    unknown = sorted({l for l in got if l not in allowed})
    return kept, unknown


def classify(client: Anthropic, full_name: str, labels: list[dict], issue: dict,
             guidelines: str, model: str = MODEL) -> CallResult:
    """Always returns a CallResult; any failure is recorded in `error`, never raised."""
    req = build_request(model, full_name, labels, issue, guidelines)
    allowed = {l["name"] for l in labels}
    res = CallResult([], model, None, None, 0, None, None)
    t0 = time.perf_counter()
    try:
        raw = client.messages.with_raw_response.create(**req)
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
            res.prompt_tokens = resp.usage.input_tokens
            res.completion_tokens = resp.usage.output_tokens
        if resp.stop_reason == "refusal":
            raise ValueError("refusal")
        text = "".join(b.text for b in resp.content if b.type == "text")
        if not text:
            raise ValueError(f"no text in reply (stop_reason={resp.stop_reason})")
        res.labels, res.unknown_labels = parse_labels(text, allowed)
    except Exception as e:
        res.labels, res.unknown_labels = [], []
        res.error = f"unparseable reply: {type(e).__name__}: {e}"[:500]
    return res
