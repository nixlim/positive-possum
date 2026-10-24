import json
from datetime import UTC, datetime, timedelta

from triage import cli
from triage import provenance as pv
from triage.agreement import agreement
from triage.llm import CallResult
from triage.store import Store

NOW = datetime(2026, 10, 24, 12, tzinfo=UTC)
MODEL = "claude-sonnet-5-5"
# Header names and shapes copied from the main checkout's log (ids redacted).
HEADERS = {
    "content-type": "application/json",
    "anthropic-ratelimit-input-tokens-limit": "10000000",
    "anthropic-ratelimit-input-tokens-remaining": "9999000",
    "anthropic-ratelimit-requests-limit": "10000",
    "anthropic-ratelimit-requests-remaining": "9999",
    "anthropic-ratelimit-tokens-reset": "2026-10-23T19:49:38Z",
    "request-id": "req_011CgKkPkXXXXXXXXXXXXXXX",
    "anthropic-organization-id": "00000000-0000-0000-0000-000000000000",
    "server-timing": "x-originResponse;dur=1822",
    "x-robots-tag": "none",
    "cf-ray": "0000000000000000-AMS",
    "x-middleware-request-id": "mreq_00000000000000000000000000000000",
    "via": "1.1 google",
}
MUBIT_ONLY = {k: v for k, v in HEADERS.items() if k in ("content-type", "x-middleware-request-id", "via")}
# The shape seen on an errored reply: organization id and request id, no rate-limit headers.
NO_RATELIMIT = {k: v for k, v in HEADERS.items() if not k.startswith("anthropic-ratelimit-")}
USAGE = {
    "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0},
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
    "inference_geo": "global",
    "input_tokens": 1495,
    "output_tokens": 18,
    "output_tokens_details": {"thinking_tokens": 0},
}
PROVIDER_FP = ("anthropic-organization-id", "anthropic-ratelimit-*", "request-id: req_")
GATEWAY_429 = "HTTP 429: Error code: 429 - {'error': 'call cap reached'}"


def row(i, hours_ago, repo="enwiki", served=MODEL, headers=None, latency=None, out=None, error=None, **cols):
    gateway = pv.error_group(error) == "gateway"
    r = {
        "id": i,
        "ts": (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
        "run_id": "run-1",
        "kind": "live",
        "repo": repo,
        "issue": i,
        "model_asked": MODEL,
        "model_served": None if gateway else served,
        "latency_ms": 1500 + 100 * (i % 10) if latency is None else latency,
        "prompt_tokens": None if error else 1000,
        "completion_tokens": None if error else (20 + i % 5 if out is None else out),
        "error": error,
        "headers": dict(headers if headers is not None else MUBIT_ONLY if gateway else HEADERS),
        "raw": {"status_code": 429, "body": {}} if gateway else {"id": "msg_011CgKk", "usage": USAGE},
        "extra": {},
    }
    return {**r, **cols}


def log(n_base=40, n_recent=40, repo="enwiki", start=0, **recent):
    """`n_base` provider calls two days ago, then `n_recent` within the last hours."""
    base = [row(start + i, 48 - i / 60, repo) for i in range(n_base)]
    rec = [row(start + n_base + i, 5 - i / 60, repo, **recent) for i in range(n_recent)]
    return base + rec


def test_fingerprint_of_a_provider_reply():
    fp = pv.fingerprint(row(1, 1))
    assert fp == {
        "served": MODEL,
        "provider": True,
        "provider_misses": (),
        "provider_headers": PROVIDER_FP,
        "ratelimit": True,
        "id_prefix": "msg_",
        "usage_keys": tuple(sorted(USAGE)),
        "origin_ms": 1822.0,
        "latency_ms": 1600,
        "input_tokens": 1000,
        "output_tokens": 21,
        "ok": True,
        "error": None,
    }


def test_header_names_are_case_insensitive():
    upper = {"-".join(p.capitalize() for p in k.split("-")): v for k, v in HEADERS.items()}
    assert "Anthropic-Organization-Id" in upper and "Server-Timing" in upper
    assert pv.fingerprint(row(1, 1, headers=upper)) == pv.fingerprint(row(1, 1))


def test_request_id_counts_only_with_the_provider_prefix():
    headers = {**NO_RATELIMIT, "request-id": "mreq_123"}
    fp = pv.fingerprint(row(1, 1, headers=headers))
    assert fp["provider_headers"] == ("anthropic-organization-id",)
    assert fp["provider"] is False and fp["provider_misses"] == ("request-id: req_",)


def test_a_dated_provider_model_passes_and_another_model_fails():
    assert pv.fingerprint(row(1, 1, served=MODEL + "-20261001"))["provider"] is True
    fp = pv.fingerprint(row(1, 1, served="mubit-adapter-enwiki"))
    assert fp["provider"] is False and fp["provider_misses"] == ("served",)


def test_no_ratelimit_headers_still_pass_the_provider_test():
    fp = pv.fingerprint(row(1, 1, headers=NO_RATELIMIT))
    assert fp["provider"] is True and fp["ratelimit"] is False
    assert fp["provider_headers"] == ("anthropic-organization-id", "request-id: req_")
    rows = [row(i, 48 - i / 60, headers=NO_RATELIMIT) for i in range(40)]
    rows += [row(100 + i, 2, headers=NO_RATELIMIT) for i in range(5)]
    s = pv.shift(rows, baseline_n=40, now=NOW)["enwiki"]
    assert s["baseline"]["n"] == 40 and s["shift"]["hard"] == []


def test_provider_only_log_gives_no_signal():
    s = pv.shift(log(), baseline_n=40, now=NOW)["enwiki"]
    assert s["shift"] == {"hard": [], "soft": []}
    assert s["baseline"]["n"] == 40 and s["recent"]["n"] == 40
    assert [g["count"] for g in s["baseline"]["fingerprints"]] == [40]
    assert (s["requests"], s["answered"], s["errors"]) == (80, 80, {"gateway": 0, "unparseable": 0})
    assert s["served_by"] == {MODEL: 80} and s["provider_share"] == 1.0
    assert (s["tokens_in"], s["tokens_out"]) == (80_000, sum(20 + i % 5 for i in range(80)))


def test_a_different_served_model_is_a_hard_signal():
    rows = log(n_recent=10) + [row(100 + i, 2 - i / 60, served="mubit-adapter-enwiki") for i in range(3)]
    s = pv.shift(rows, baseline_n=40, now=NOW)["enwiki"]
    (hard,) = s["shift"]["hard"]
    assert hard["served"] == "mubit-adapter-enwiki" and hard["changed"] == ["served"]
    assert hard["count"] == 3 and hard["first_ts"] == rows[-3]["ts"]
    assert s["provider_share"] == 50 / 53


def test_a_non_provider_answered_row_is_hard_without_a_baseline():
    rows = [row(i, 2 - i / 60, repo="edit-classify", served="mubit-own") for i in range(4)]
    s = pv.shift(rows, baseline_n=40, now=NOW)["edit-classify"]
    assert s["baseline"]["n"] == 0 and s["recent"]["n"] == 4
    assert [(g["changed"], g["count"], g["first_ts"]) for g in s["shift"]["hard"]] == [
        (["served"], 4, rows[0]["ts"])
    ]


def test_baseline_skips_early_non_provider_rows():
    early = [row(900 + i, 49, served="mubit-own") for i in range(3)]
    rows = early + log()
    s = pv.shift(rows, baseline_n=40, now=NOW)["enwiki"]
    assert s["baseline"]["n"] == 40 and s["baseline"]["from"] == log()[0]["ts"]
    assert s["shift"]["hard"] == []  # the early rows are outside the recent window
    hard = pv.shift(rows, baseline_n=40, recent_hours=72, now=NOW)["enwiki"]["shift"]["hard"]
    assert [(g["served"], g["count"]) for g in hard] == [("mubit-own", 3)]


def test_missing_anthropic_headers_is_a_hard_signal():
    hard = pv.shift(log(n_recent=5, headers=MUBIT_ONLY), baseline_n=40, now=NOW)["enwiki"]["shift"]["hard"]
    assert [(g["changed"], g["provider_headers"], g["count"]) for g in hard] == [
        (["anthropic-organization-id", "request-id: req_", "ratelimit"], (), 5)
    ]


def test_new_reply_shape_against_the_baseline_is_hard():
    raw = {"id": "resp_1", "usage": {"input_tokens": 1, "output_tokens": 2}}
    hard = pv.shift(log(n_recent=5, raw=raw), baseline_n=40, now=NOW)["enwiki"]["shift"]["hard"]
    assert [(g["changed"], g["provider"]) for g in hard] == [(["id_prefix", "usage_keys"], True)]
    hard = pv.shift(log(n_recent=5, headers=NO_RATELIMIT), baseline_n=40, now=NOW)["enwiki"]["shift"]["hard"]
    assert [(g["changed"], g["provider"]) for g in hard] == [(["ratelimit"], True)]


def test_a_latency_drop_is_a_soft_signal():
    sig = pv.shift(log(latency=300), baseline_n=40, now=NOW)["enwiki"]["shift"]
    assert sig["hard"] == []
    (soft,) = sig["soft"]
    assert soft["metric"] == "latency_ms" and soft["direction"] == "down"
    assert soft["recent_median"] == 300 and soft["baseline_median"] == 1950
    q1, q3 = soft["baseline_iqr"]
    assert q1 > 300 and q3 < 2500


def test_recent_window_excludes_old_calls_and_the_baseline():
    rows = log(n_recent=0) + [row(200 + i, 30, served="other") for i in range(5)]
    s = pv.shift(rows, baseline_n=40, recent_hours=24, now=NOW)["enwiki"]
    assert s["recent"]["n"] == 0 and s["shift"]["hard"] == []
    assert pv.shift(rows, baseline_n=40, recent_hours=48, now=NOW)["enwiki"]["shift"]["hard"]


def test_gateway_and_unparseable_errors_are_counted_not_scored():
    errs = [row(300 + i, 49, error=GATEWAY_429) for i in range(5)]
    errs += [row(310 + i, 2, error=GATEWAY_429) for i in range(5)]
    errs += [row(320, 2, error="APITimeoutError: Request timed out.")]
    errs += [row(330 + i, 1, served="mubit-own", error="unparseable reply: ValueError: x") for i in range(5)]
    s = pv.shift(errs + log(), baseline_n=40, now=NOW)["enwiki"]
    assert s["errors"] == {"gateway": 11, "unparseable": 5}
    assert (s["requests"], s["answered"]) == (96, 80)
    assert s["unparseable_rate"] == 5 / 85
    assert s["served_by"] == {MODEL: 80} and s["provider_share"] == 1.0
    assert s["baseline"]["n"] == 40 and s["baseline"]["from"] == log()[0]["ts"]
    assert s["recent"]["n"] == 40 and s["shift"] == {"hard": [], "soft": []}


def test_steps_are_compared_separately():
    # Each step's baseline is its own: a reply shape normal for one step is new for the other.
    resp = {"id": "resp_1", "usage": USAGE}
    rows = log(repo="edit-extract", n_recent=5, raw=resp) + log(repo="enwiki", start=1000)
    rows += [row(2000 + i, 48, repo="edit-classify", raw=resp) for i in range(40)]
    rows += [row(3000 + i, 1, repo="edit-classify", raw=resp) for i in range(5)]
    out = pv.shift(rows, baseline_n=40, now=NOW)
    assert out["edit-extract"]["shift"]["hard"][0]["changed"] == ["id_prefix"]
    assert out["enwiki"]["shift"]["hard"] == []
    assert out["edit-classify"]["shift"]["hard"] == []


def test_hourly_counts_per_step_and_hour():
    rows = [row(1, 1.5), row(2, 1.4, served="mubit-own"), row(3, 1.3, error="HTTP 500: x"), row(4, 0.2)]
    rows += [row(6, 1.2, error="unparseable reply: ValueError: x"), row(5, 0.2, repo="godot")]
    hrs = pv.hourly(rows)
    first, second = hrs["enwiki"]
    assert first["hour"] == "2026-10-24T10:00Z"
    assert (first["requests"], first["answered"], first["errors"]) == (4, 2, {"gateway": 1, "unparseable": 1})
    assert (first["provider_share"], first["fingerprints"], first["latency_p50_ms"]) == (0.5, 2, 1650)
    assert (second["requests"], second["provider_share"]) == (1, 1.0)
    assert [h["requests"] for h in hrs["godot"]] == [1]


def test_report_keeps_the_recent_hours_and_renders():
    rows = log(n_recent=5) + [row(100, 1, served="mubit-own", headers=MUBIT_ONLY)]
    rows.append(row(101, 1, error=GATEWAY_429))
    rep = pv.report(rows, baseline_n=40, hours=24, now=NOW)
    assert all(h["hour"] >= "2026-10-23T12:00Z" for h in rep["enwiki"]["hourly"])
    assert len(rep["enwiki"]["hourly"]) == 2
    assert rep["enwiki"]["agreement"] == {"extract": None, "classify": None, "chained": None}
    text = pv.render(rep)
    assert "== enwiki: 47 requests, 46 answered; errors (never scored): gateway 1, unparseable 0" in text
    assert "provider test (served is the asked model" in text and "45/46 answered pass" in text
    assert "baseline 40 provider-served calls" in text and "HARD     1" in text
    assert "changed=served,anthropic-organization-id,request-id: req_,ratelimit" in text
    assert "agreement with baseline output: extract -, classify -, chained -" in text
    assert "no signal" in pv.render(pv.report(log(), 40, 24, NOW))
    json.dumps(rep)  # the API serves this as is


EXTRACT = {"claims_added": ["a"], "claims_removed": [], "negative_info": "none", "tone_shift": 0}


def ext(i, hours_ago, revid, kind="pipeline", run_id="batch", error=None, **extra):
    fields = {} if error else {**EXTRACT, **extra}
    return row(i, hours_ago, "edit-extract", kind=kind, run_id=run_id, issue=revid, error=error, extra=fields)


def test_extract_agreement_uses_the_latest_probe_run_and_answered_revids():
    rows = [ext(1, 30, 1), ext(2, 30, 2), ext(3, 30, 3), ext(4, 30, 4, error=GATEWAY_429)]
    rows += [ext(5, 29, 4), ext(6, 29, 6, error="unparseable reply: ValueError: x")]
    rows += [ext(10 + r, 20, r, "probe", "p1", tone_shift=2) for r in (1, 2, 3, 4)]
    rows += [ext(20, 2, 1, "probe", "p2"), ext(21, 2, 2, "probe", "p2", tone_shift=1)]
    rows += [ext(22, 2, 3, "probe", "p2", error=GATEWAY_429), ext(23, 2, 4, "probe", "p2")]
    rows += [ext(24, 2, 6, "probe", "p2"), ext(25, 2, 7, "probe", "p2")]
    # Revid 3 errored in the probe, 6 in the baseline, 7 has no baseline; 4 was answered on retry.
    assert agreement(rows)["edit-extract"] == {
        "extract": {"value": 2 / 3, "n": 3, "run_id": "p2", "ts": NOW_2H},
        "classify": None,
        "chained": None,
    }


def test_a_fully_errored_latest_run_is_not_reported_as_the_previous_one():
    rows = [ext(1, 30, 1), ext(2, 30, 2), ext(10, 20, 1, "probe", "p1"), ext(11, 20, 2, "probe", "p1")]
    rows += [ext(20 + r, 2, r, "probe", "p2", error=GATEWAY_429) for r in (1, 2)]
    rows += [cls(30, 1, extract_call_id=1), cls(31, 1, "probe", "p1", extract_call_id=1)]
    rows += [row(32, 1, "edit-classify", kind="probe", run_id="p2", issue=1, error=GATEWAY_429)]
    a = agreement(rows)
    assert a["edit-extract"]["extract"] == {"value": None, "n": 0, "run_id": "p2", "ts": NOW_2H}
    assert a["edit-classify"] == {"extract": None, "classify": None, "chained": None}


LABEL = {"label": "content", "severity": 0, "confidence": "high", "review": False, "rationale": "x"}
NOW_2H = "2026-10-24T10:00:00+00:00"


def cls(i, revid, kind="pipeline", run_id="batch", **extra):
    hours_ago = 2 if kind == "probe" else 30
    fields = {**LABEL, **extra}
    return row(i, hours_ago, "edit-classify", kind=kind, run_id=run_id, issue=revid, extra=fields)


def test_classify_and_chained_agreement_are_split_by_the_input_extract():
    # Baseline extracts 31-33, probe extracts 41-43; classify rows say only which one they read.
    rows = [ext(30 + r, 31, r) for r in (1, 2, 3)] + [ext(40 + r, 3, r, "probe", "p1") for r in (1, 2, 3)]
    rows += [cls(1, 1, extract_call_id=31), cls(2, 2, review_model=False, review=True, extract_call_id=32)]
    rows += [cls(3, 3, extract_call_id=33)]
    rows += [cls(10 + r, r, "probe", "p1", extract_call_id=30 + r) for r in (1, 3)]
    rows += [cls(12, 2, "probe", "p1", review_model=False, review=True, extract_call_id=32)]
    rows += [cls(20, 1, "probe", "p1", label="whitewash", extract_call_id=41)]
    # The model's own review is compared: the forced review on the baseline is not a difference.
    rows += [cls(21, 2, "probe", "p1", review=False, extract_call_id=42)]
    rows += [cls(22, 3, "probe", "p1", rationale="other words", extract_call_id=43)]
    # No input recorded, or an input not in the log: left out of both metrics.
    rows += [
        cls(23, 1, "probe", "p1", label="spam"),
        cls(24, 2, "probe", "p1", label="spam", extract_call_id=99),
    ]
    assert agreement(rows)["edit-classify"] == {
        "extract": None,
        "classify": {"value": 1.0, "n": 3, "run_id": "p1", "ts": NOW_2H},
        "chained": {"value": 2 / 3, "n": 3, "run_id": "p1", "ts": NOW_2H},
    }


def test_agreement_is_null_without_probes():
    rows = [cls(1, 1), cls(2, 2), ext(3, 30, 3), row(4, 1)]
    rows.append(row(5, 2, "edit-classify", kind="probe", run_id="p1", issue=1, error=GATEWAY_429))
    assert agreement(rows) == {
        step: {"extract": None, "classify": None, "chained": None}
        for step in ("edit-classify", "edit-extract", "enwiki")
    }


def test_read_calls_and_cli_on_a_logged_store(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("triage.store.DATA_DIR", tmp_path)
    store = Store()
    raw = {"id": "msg_1", "usage": USAGE}
    ok = CallResult(["keep"], MODEL, MODEL, "mreq_1", 900, 1495, 18, headers=HEADERS, raw=raw)
    ok.extra = {"reason": "fine"}
    failed = CallResult([], MODEL, None, None, 5, None, None, error="APITimeoutError: timed out")
    store.log_call("live-enwiki", "live", "enwiki", 1, ok)
    store.log_call("live-enwiki", "live", "enwiki", 2, failed)
    store.log_call("x", "pipeline", "edit-extract", 3, ok)
    rows = pv.read_calls(store, step="enwiki")
    assert [(r["id"], r["repo"], r["issue"], r["run_id"]) for r in rows] == [
        (1, "enwiki", 1, "live-enwiki"),
        (2, "enwiki", 2, "live-enwiki"),
    ]
    assert rows[0]["extra"] == {"reason": "fine"} and rows[1]["extra"] == {}
    assert pv.fingerprint(rows[0])["provider"] is True
    assert pv.fingerprint(rows[1])["error"] == "gateway"
    cli.main(["provenance", "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert set(rep) == {"enwiki", "edit-extract"}
    e = rep["enwiki"]
    assert (e["requests"], e["answered"], e["errors"]) == (2, 1, {"gateway": 1, "unparseable": 0})
    assert (e["tokens_in"], e["tokens_out"], e["served_by"]) == (1495, 18, {MODEL: 1})
    for key in ("provider_share", "latency_p50_ms", "shift", "agreement"):
        assert key in e
    cli.main(["provenance", "--step", "edit-extract"])
    out = capsys.readouterr().out
    assert "== edit-extract: 1 requests, 1 answered" in out
    assert "baseline 1 provider-served calls" in out
