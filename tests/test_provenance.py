import json
from datetime import UTC, datetime, timedelta

from triage import cli
from triage import provenance as pv
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


def row(i, hours_ago, repo="enwiki", served=MODEL, headers=None, latency=None, out=None, error=None):
    return {
        "id": i,
        "ts": (NOW - timedelta(hours=hours_ago)).isoformat(timespec="seconds"),
        "repo": repo,
        "kind": "live",
        "model_asked": MODEL,
        "model_served": None if error else served,
        "latency_ms": 1500 + 100 * (i % 10) if latency is None else latency,
        "completion_tokens": None if error else (20 + i % 5 if out is None else out),
        "error": error,
        "headers": {} if error else dict(HEADERS if headers is None else headers),
        "raw": None if error else {"id": "msg_011CgKkPmXXXX", "model": served, "usage": USAGE},
    }


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
        "provider_headers": PROVIDER_FP,
        "id_prefix": "msg_",
        "usage_keys": tuple(sorted(USAGE)),
        "origin_ms": 1822.0,
        "latency_ms": 1600,
        "output_tokens": 21,
        "ok": True,
    }


def test_header_names_are_case_insensitive():
    upper = {"-".join(p.capitalize() for p in k.split("-")): v for k, v in HEADERS.items()}
    assert "Anthropic-Organization-Id" in upper and "Server-Timing" in upper
    assert pv.fingerprint(row(1, 1, headers=upper)) == pv.fingerprint(row(1, 1))


def test_request_id_counts_only_with_the_provider_prefix():
    headers = {**MUBIT_ONLY, "request-id": "mreq_123"}
    assert pv.fingerprint(row(1, 1, headers=headers))["provider_headers"] == ()


def test_provider_only_log_gives_no_signal():
    s = pv.shift(log(), baseline_n=40, now=NOW)["enwiki"]
    assert s["shift"] == {"hard": [], "soft": []}
    assert s["baseline"]["n"] == 40 and s["recent"]["n"] == 40
    assert [g["count"] for g in s["baseline"]["fingerprints"]] == [40]


def test_a_different_served_model_is_a_hard_signal():
    rows = log(n_recent=10) + [row(100 + i, 2 - i / 60, served="mubit-adapter-enwiki") for i in range(3)]
    hard = pv.shift(rows, baseline_n=40, now=NOW)["enwiki"]["shift"]["hard"]
    assert len(hard) == 1
    assert hard[0]["served"] == "mubit-adapter-enwiki" and hard[0]["changed"] == ["served"]
    assert hard[0]["count"] == 3 and hard[0]["first_ts"] == rows[-3]["ts"]


def test_missing_anthropic_headers_is_a_hard_signal():
    hard = pv.shift(log(n_recent=5, headers=MUBIT_ONLY), baseline_n=40, now=NOW)["enwiki"]["shift"]["hard"]
    assert [(g["changed"], g["provider_headers"], g["count"]) for g in hard] == [
        (["provider_headers"], (), 5)
    ]


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


def test_errors_are_excluded_from_baseline_and_recent():
    errs = [row(300 + i, 49, error="HTTP 529: overloaded") for i in range(5)]
    errs += [row(400 + i, 1, error="unparseable reply: ValueError") for i in range(5)]
    s = pv.shift(errs + log(), baseline_n=40, now=NOW)["enwiki"]
    assert s["errors_excluded"] == 10
    assert s["baseline"]["n"] == 40 and s["baseline"]["from"] == log()[0]["ts"]
    assert s["recent"]["n"] == 40 and s["shift"] == {"hard": [], "soft": []}


def test_steps_are_compared_separately():
    # Each step's baseline is its own: a served string normal for one step is new for the other.
    rows = log(repo="edit-extract", start=0, served="mubit-own") + log(repo="enwiki", start=1000)
    rows += [row(2000 + i, 48, repo="edit-classify", served="mubit-own") for i in range(40)]
    rows += [row(3000 + i, 1, repo="edit-classify", served="mubit-own") for i in range(5)]
    out = pv.shift(rows, baseline_n=40, now=NOW)
    assert out["edit-extract"]["shift"]["hard"][0]["served"] == "mubit-own"
    assert out["enwiki"]["shift"]["hard"] == []
    assert out["edit-classify"]["shift"]["hard"] == []


def test_hourly_counts_per_step_and_hour():
    rows = [row(1, 1.5), row(2, 1.4, served="mubit-own"), row(3, 1.3, error="HTTP 500"), row(4, 0.2)]
    rows.append(row(5, 0.2, repo="godot"))
    hrs = pv.hourly(rows)
    first, second = hrs["enwiki"]
    assert first["hour"] == "2026-10-24T10:00Z"
    assert (first["calls"], first["errors"], first["provider_share"], first["fingerprints"]) == (3, 1, 0.5, 2)
    assert first["p50_latency_ms"] == 1650
    assert (second["calls"], second["provider_share"]) == (1, 1.0)
    assert [h["calls"] for h in hrs["godot"]] == [1]


def test_report_keeps_the_recent_hours_and_renders():
    rows = log(n_recent=5) + [row(100, 1, served="mubit-own", headers=MUBIT_ONLY)]
    rep = pv.report(rows, baseline_n=40, hours=24, now=NOW)
    assert all(h["hour"] >= "2026-10-23T12:00Z" for h in rep["enwiki"]["hourly"])
    assert len(rep["enwiki"]["hourly"]) == 2
    text = pv.render(rep)
    assert "baseline 40 answered" in text and "HARD     1" in text
    assert "changed=served,provider_headers" in text
    assert "no signal" in pv.render(pv.report(log(), 40, 24, NOW))
    json.dumps(rep)  # the API serves this as is


def test_read_calls_and_cli_on_a_logged_store(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr("triage.store.DATA_DIR", tmp_path)
    store = Store()
    raw = {"id": "msg_1", "usage": USAGE}
    ok = CallResult(["keep"], MODEL, MODEL, "mreq_1", 900, 1495, 18, headers=HEADERS, raw=raw)
    failed = CallResult([], MODEL, None, None, 5, None, None, error="Timeout")
    store.log_call("live-enwiki", "live", "enwiki", 1, ok)
    store.log_call("live-enwiki", "live", "enwiki", 2, failed)
    store.log_call("x", "pipeline", "edit-extract", 3, ok)
    rows = pv.read_calls(store, step="enwiki")
    assert [(r["id"], r["repo"]) for r in rows] == [(1, "enwiki"), (2, "enwiki")]
    assert pv.fingerprint(rows[0])["provider_headers"] == PROVIDER_FP
    assert pv.fingerprint(rows[1])["ok"] is False
    cli.main(["provenance", "--json"])
    rep = json.loads(capsys.readouterr().out)
    assert set(rep) == {"enwiki", "edit-extract"} and rep["enwiki"]["errors_excluded"] == 1
    cli.main(["provenance", "--step", "edit-extract"])
    assert "== edit-extract: baseline 1 answered" in capsys.readouterr().out
