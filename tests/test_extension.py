"""Static checks on the Chrome extension: manifest scope, fixtures against the API contract, no HTML injection."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

EXT = Path(__file__).parent.parent / "extension"
JS_FILES = sorted(EXT.rglob("*.js"))
STEPS = {"edit-extract", "edit-classify", "page-brief"}
LABELS = {
    "content",
    "maintenance",
    "sourcing",
    "promotional",
    "unsourced_claim",
    "whitewash",
    "smear",
    "vandalism",
    "unclear",
}

# Contract shapes from PIPELINE.md section 9 (and the section 5 fields PageAnalysis carries).
PAGE_KEYS = {
    "title",
    "pageid",
    "as_of",
    "days",
    "computed_at",
    "risk_score",
    "risk_partial",
    "risk_parts",
    "analytics",
    "structure",
    "editors",
    "flags",
}
ANALYTICS_KEYS = {
    "edits_90d",
    "edits_30d",
    "edits_per_day",
    "burst_days",
    "top3_share",
    "editors_90d",
    "spa_count",
    "new_account_share",
    "ip_share",
    "temp_share",
    "reverted_30d",
    "reverted_90d",
    "flagged_30d",
}
STRUCTURE_KEYS = {
    "sections_then",
    "sections_now",
    "controversy_rank_then",
    "controversy_rank_now",
    "controversy_words_then",
    "controversy_words_now",
    "controversy_share_then",
    "controversy_share_now",
    "words_then",
    "words_now",
    "bloat_index",
    "negative_words_removed",
}
EDITOR_KEYS = {"name", "kind", "edits", "share", "account_age_days", "edit_count", "spa", "reverted"}
EDIT_KEYS = {
    "revid",
    "parentid",
    "timestamp",
    "title",
    "editor_kind",
    "comment",
    "size_delta",
    "reverted",
    "reverting",
    "diff_url",
    "measured",
    "extract",
    "classification",
    "served",
}
MEASURED_KEYS = {"words_added", "words_removed", "refs_added", "refs_removed", "sections"}
EXTRACT_KEYS = {"claims_added", "claims_removed", "negative_info", "tone_shift"}
CLASSIFICATION_KEYS = {
    "label",
    "severity",
    "review",
    "review_model",
    "review_forced",
    "confidence",
    "rationale",
    "source",
}
FORCED_RULES = {"refs_removed", "net_words_removed", "controversy_words_removed"}
STEP_KEYS = {
    "requests",
    "answered",
    "errors",
    "served_by",
    "provider_share",
    "latency_p50_ms",
    "tokens_in",
    "tokens_out",
    "shift",
    "agreement",
    "console",
}
HARD_SIGNAL_KEYS = {"served", "provider_headers", "id_prefix", "usage_keys", "count", "first_ts", "changed"}
SOFT_SIGNAL_KEYS = {"metric", "direction", "recent_median", "baseline_median", "baseline_iqr", "n_recent"}
BRIEF_KEYS = {"brief_md", "generated_at", "model_served"}
WATCHLIST_KEYS = {
    "title",
    "pageid",
    "risk_score",
    "risk_partial",
    "flagged_30d",
    "edits_30d",
    "reverted_30d",
    "last_edit_at",
    "spark",
}


def _json(name: str):
    return json.loads((EXT / name).read_text())


def _missing(keys: set, obj: dict) -> set:
    return keys - obj.keys()


def test_manifest_is_mv3_with_only_the_listed_permissions():
    m = _json("manifest.json")
    assert m["manifest_version"] == 3
    assert m["permissions"] == ["storage"]
    assert set(m["host_permissions"]) == {"https://en.wikipedia.org/*", "http://127.0.0.1:8322/*"}
    assert "optional_permissions" not in m and "optional_host_permissions" not in m
    (script,) = m["content_scripts"]
    assert script["matches"] == ["https://en.wikipedia.org/wiki/*"]
    assert "web_accessible_resources" not in m
    assert "content_security_policy" not in m  # the MV3 default forbids remote and inline code


def test_manifest_files_exist_and_no_remote_scripts():
    m = _json("manifest.json")
    referenced = [m["background"]["service_worker"], m["options_page"], *m["content_scripts"][0]["js"]]
    for name in referenced:
        assert (EXT / name).is_file(), name
    for html in EXT.rglob("*.html"):
        srcs = re.findall(r'<script[^>]*\bsrc="([^"]+)"', html.read_text())
        assert srcs and all(not s.startswith(("http:", "https:", "//")) for s in srcs), html
    assert 'importScripts("shared.js")' in (EXT / "background.js").read_text()


def test_page_fixture_carries_the_page_analysis_contract():
    page = _json("fixtures/page.json")
    assert not _missing(PAGE_KEYS, page)
    assert not _missing(ANALYTICS_KEYS, page["analytics"])
    assert not _missing(STRUCTURE_KEYS, page["structure"])
    for snap in (page["structure"]["sections_then"], page["structure"]["sections_now"]):
        assert not _missing({"revid", "timestamp", "sections"}, snap)
        for section in snap["sections"]:
            assert not _missing({"title", "level", "words"}, section)
    for editor in page["editors"]:
        assert not _missing(EDITOR_KEYS, editor), editor["name"]
    for edit in page["flags"]:
        assert not _missing(EDIT_KEYS, edit), edit["revid"]
        assert not _missing(MEASURED_KEYS, edit["measured"])
        assert not _missing(EXTRACT_KEYS, edit["extract"])
        assert not _missing(CLASSIFICATION_KEYS, edit["classification"])
        assert set(edit["served"]) == {"extract", "classify"}


def test_page_fixture_is_synthetic_and_internally_consistent():
    page = _json("fixtures/page.json")
    a, st = page["analytics"], page["structure"]
    assert page["title"].endswith("(fixture)")
    assert all(e["name"].startswith("Fixture-") or e["kind"] != "registered" for e in page["editors"])
    days = a["edits_per_day"]
    assert len(days) == page["days"] == 90
    assert sum(d["count"] for d in days) == a["edits_90d"] == sum(e["edits"] for e in page["editors"])
    assert sum(d["count"] for d in days[-30:]) == a["edits_30d"]
    dates = {d["date"] for d in days}
    assert a["burst_days"] and set(a["burst_days"]) <= dates
    flags = page["flags"]
    assert len(flags) == 5
    assert len({f["classification"]["label"] for f in flags}) >= 4
    assert {f["classification"]["label"] for f in flags} <= LABELS
    for f in flags:
        assert f["timestamp"][:10] in dates
        assert f["title"] == page["title"]
        assert (
            f["diff_url"] == f"https://en.wikipedia.org/w/index.php?diff={f['revid']}&oldid={f['parentid']}"
        )
    assert st["controversy_rank_now"] > st["controversy_rank_then"]
    rank = st["controversy_rank_now"]
    assert st["sections_now"]["sections"][rank]["title"] == "Expenses inquiry"
    assert sum(s["words"] for s in st["sections_then"]["sections"]) == st["words_then"]
    assert sum(s["words"] for s in st["sections_now"]["sections"]) == st["words_now"]
    assert sum(page["risk_parts"].values()) == pytest.approx(page["risk_score"])


def _rule_holds(rule: str, m: dict) -> bool:
    if rule == "refs_removed":
        return m["refs_removed"] >= 1
    if rule == "net_words_removed":
        return m["words_removed"] - m["words_added"] >= 50
    controversy = any(re.search(r"controvers|criticism|expenses|inquiry", s, re.I) for s in m["sections"])
    return m["words_removed"] > 0 and controversy


def test_page_fixture_forced_review_follows_the_measured_facts():
    flags = _json("fixtures/page.json")["flags"]
    for f in flags:
        c = f["classification"]
        assert c["review_forced"] is None or c["review_forced"] in FORCED_RULES
        assert c["review"] == (c["review_model"] or c["review_forced"] is not None)
        if c["review_forced"]:
            assert _rule_holds(c["review_forced"], f["measured"]), f["revid"]
    assert any(
        f["classification"]["review_forced"] and f["classification"]["review_model"] is False for f in flags
    )


def test_status_fixture_has_every_step_served_by_the_provider_and_no_shift():
    status = _json("fixtures/status.json")
    assert set(status) == STEPS
    for step, s in status.items():
        assert not _missing(STEP_KEYS, s), step
        assert set(s["errors"]) == {"gateway", "unparseable"}
        assert s["answered"] + s["errors"]["gateway"] + s["errors"]["unparseable"] == s["requests"], step
        assert set(s["served_by"]) == {"claude-sonnet-5-5"}
        assert sum(s["served_by"].values()) == s["answered"]
        assert s["provider_share"] == 1.0
        assert s["shift"] == {"hard": [], "soft": []}
        assert set(s["agreement"]) == {"extract", "classify", "chained"}
        for a in s["agreement"].values():
            assert a is None or (set(a) == {"value", "n"} and 0 <= a["value"] <= 1 and a["n"] > 0)
    assert set(status["page-brief"]["agreement"].values()) == {None}


def test_mubit_tab_renders_every_shift_signal_field():
    src = (EXT / "content" / "mubit.js").read_text()
    for key in HARD_SIGNAL_KEYS | SOFT_SIGNAL_KEYS:
        assert f"s.{key}" in src, key
    assert "drift through the chain (not identical input)" in src
    assert "Mubit call budget for this session is spent" in src


def test_background_posts_only_for_brief_generation_and_refresh():
    src = (EXT / "background.js").read_text()
    routes = re.findall(r'^\s*"?([\w-]+)"?: \{ method: "(GET|POST)", path: "([^"]+)"', src, re.M)
    assert {name for name, _, _ in routes} == {"page", "brief", "status", "brief-generate", "refresh"}
    posts = {name: path for name, method, path in routes if method == "POST"}
    assert posts == {"brief-generate": "/api/page/brief", "refresh": "/api/page/refresh"}
    assert src.count('"POST"') == 2
    assert src.count("fetch(") == 2  # one for bundled fixtures, one for the backend
    assert "method: route.method" in src


def test_only_the_service_worker_makes_network_calls():
    for path in JS_FILES:
        if path.name != "background.js":
            assert not re.search(
                r"\bfetch\(|XMLHttpRequest|WebSocket|EventSource|sendBeacon", path.read_text()
            ), path


def test_brief_and_watchlist_fixtures_carry_their_contract():
    brief = _json("fixtures/brief.json")
    assert not _missing(BRIEF_KEYS, brief)
    for heading in ("What changed", "Who", "Pattern", "Why now", "Suggested checks"):
        assert f"## {heading}\n" in brief["brief_md"]
    assert brief["brief_md"].endswith("Signals for human review, not findings.")
    watchlist = _json("fixtures/watchlist.json")
    assert watchlist
    for row in watchlist:
        assert not _missing(WATCHLIST_KEYS, row), row["title"]
        assert row["title"].endswith("(fixture)")
        assert len(row["spark"]) == 30


def test_js_never_parses_html_or_evaluates_strings():
    assert JS_FILES
    banned = re.compile(
        r"innerHTML|outerHTML|insertAdjacentHTML|document\.write|\beval\s*\(|new\s+Function\b"
    )
    for path in JS_FILES:
        hits = banned.findall(path.read_text())
        assert not hits, (path.name, hits)


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
@pytest.mark.parametrize("path", JS_FILES, ids=lambda p: str(p.relative_to(EXT)))
def test_js_parses_with_node(path):
    r = subprocess.run(["node", "--check", str(path)], capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stderr
