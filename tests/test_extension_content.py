"""The extension's content scripts run in node against a fake DOM (tests/content_harness.js)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
HARNESS = Path(__file__).parent / "content_harness.js"
PAGE = json.loads((ROOT / "extension" / "fixtures" / "page.json").read_text())
TITLE = "Keir Starmer"
UNREACHABLE = {
    "data": None,
    "offline": False,
    "error": "backend unreachable",
    "backend": "http://127.0.0.1:8322",
}
NOT_FOUND = {"data": None, "offline": False, "status": 404, "error": "HTTP 404", "reason": None}
BUDGET_SPENT = {"data": None, "offline": False, "status": 429, "error": "HTTP 429", "reason": None}
RECENT = {**BUDGET_SPENT, "reason": "recent"}
WIKIPEDIA_SPENT = {**BUDGET_SPENT, "reason": "wikipedia"}
SERVER_ERROR = {"data": None, "offline": False, "status": 500, "error": "HTTP 500", "reason": None}
NOT_INGESTING = {"data": None, "offline": False, "error": "not ingesting"}
NOTICE = (
    "Edit watch: no backend at http://127.0.0.1:8322. Start it with uv run triage api, "
    "or turn on demo mode in Options."
)
COST = "at most four Mubit calls per queued edit per click: extract, classify and one retry of each"
FLAGGED = PAGE["analytics"]["flagged_30d"]
STATUS = json.loads((ROOT / "extension" / "fixtures" / "status.json").read_text())
# Script helpers: open the drawer on a tab, and read the drawer or banner text.
HELPERS = (
    "const host = (id) => document.getElementById(id);"
    "const drawerText = () => text(host('edit-watch-drawer').shadow);"
    "const bannerText = () => text(host('edit-watch-banner').shadow);"
    "const open = async (tab) => {"
    " await click(find(host('edit-watch-banner').shadow, 'button', 'Open'));"
    " await click(find(host('edit-watch-drawer').shadow, 'button', tab)); };"
)
TEN_SECONDS = 10 * 1000
CLICK_CLASSIFY = (
    "await open('Flags');"
    "await click(find(host('edit-watch-drawer').shadow, 'button', 'Classify recent edits'));"
)


def content(replies: dict, script: str | None = None) -> dict:
    # Node is a gate requirement: these tests fail, never skip, without it.
    assert shutil.which("node"), "node is required to run the extension tests"
    scenario = {"replies": replies, "script": HELPERS + script if script else None}
    r = subprocess.run(
        ["node", str(HARNESS), str(ROOT / "extension"), json.dumps(scenario)],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def page(**overrides) -> dict:
    return {"data": {**PAGE, **overrides}, "offline": False, "status": 200}


def sent(out: dict, kind: str) -> list[dict]:
    return [m for m in out["sent"] if m["type"] == kind]


def test_an_unreachable_backend_mounts_only_a_one_line_notice():
    out = content({"page": UNREACHABLE, "status": UNREACHABLE})
    assert out["hosts"] == [{"id": "edit-watch-notice", "text": NOTICE}]


def test_footer_and_mubit_tab_say_why_status_is_missing():
    out = content(
        {},
        "const res = " + json.dumps(UNREACHABLE) + ";"
        "return [text(footerView(res)), text(renderMubit(null, { status: res }))];",
    )
    footer, tab = out["value"]
    assert footer == "Mubit status unavailable: backend unreachable"
    assert tab == "Mubit status unavailable: backend unreachable."
    assert "provider" not in footer


@pytest.mark.parametrize(
    ("reply", "reason"),
    [(UNREACHABLE, "backend unreachable"), (BUDGET_SPENT, "Mubit call budget for this session is spent")],
)
def test_a_failed_brief_request_says_why(reply, reason):
    script = (
        "const view = renderBrief(null, { title: 'Keir Starmer', send }); await settle();"
        "const before = text(view); await click(find(view, 'button')); return [before, text(view)];"
    )
    before, after = content({"brief": NOT_FOUND, "brief-generate": reply}, script)["value"]
    assert "No brief for this page yet." in before
    assert "Brief request failed: " + reason in after
    assert "What changed" not in after


def line_text(text: str, retry: bool) -> str:
    return "Edit watch: " + text + ("Try again" if retry else "")


def test_a_first_visit_mounts_the_reading_banner_and_sends_ingest_once():
    out = content({"page": NOT_FOUND, "status": UNREACHABLE, "ingest": "pending"})
    assert out["hosts"] == [
        {"id": "edit-watch-line", "text": "Edit watch: reading this article's history from Wikipedia…"}
    ]
    assert sent(out, "ingest") == [{"type": "ingest", "title": TITLE}]


def test_a_successful_ingest_mounts_the_banner_and_drawer():
    out = content({"page": NOT_FOUND, "status": UNREACHABLE, "ingest": page()})
    assert [h["id"] for h in out["hosts"]] == ["edit-watch-drawer", "edit-watch-banner"]
    assert "23 edits / 30d" in out["hosts"][1]["text"]


@pytest.mark.parametrize(
    ("reply", "line", "retry"),
    [
        (NOT_FOUND, "Not covered: not an article Wikipedia knows", False),
        (RECENT, "Refreshed in the last 10 minutes; try again later", True),
        (WIKIPEDIA_SPENT, "Not read: Wikipedia request budget spent", True),
        (BUDGET_SPENT, "Could not reach the triage backend", True),
        (SERVER_ERROR, "Could not reach the triage backend", True),
        (UNREACHABLE, "Could not reach the triage backend", True),
        (NOT_INGESTING, "Could not reach the triage backend", True),
    ],
    ids=["404", "429-recent", "429-wikipedia", "429-no-reason", "500", "unreachable", "worker-refused"],
)
def test_a_failed_ingest_shows_one_line_saying_why_and_no_drawer(reply, line, retry):
    out = content({"page": NOT_FOUND, "status": UNREACHABLE, "ingest": reply})
    assert out["hosts"] == [{"id": "edit-watch-line", "text": line_text(line, retry)}]


def test_try_again_sends_one_refresh_per_click_and_mounts_the_banner_when_it_works():
    replies = {
        "page": NOT_FOUND,
        "status": UNREACHABLE,
        "ingest": SERVER_ERROR,
        "refresh": [RECENT, page()],
    }
    script = (
        "const retry = () => click(find(host('edit-watch-line').shadow, 'button', 'Try again'));"
        "await retry(); const after_one = text(host('edit-watch-line').shadow);"
        "await retry(); return after_one;"
    )
    out = content(replies, script)
    assert sent(out, "ingest") == [{"type": "ingest", "title": TITLE}]
    assert sent(out, "refresh") == [{"type": "refresh", "title": TITLE}] * 2
    assert out["value"] == line_text("Refreshed in the last 10 minutes; try again later", True)
    assert [h["id"] for h in out["hosts"]] == ["edit-watch-drawer", "edit-watch-banner"]


def test_a_404_offers_no_try_again_and_sends_no_refresh():
    out = content(
        {"page": NOT_FOUND, "status": UNREACHABLE, "ingest": NOT_FOUND},
        "return find(host('edit-watch-line').shadow, 'button');",
    )
    assert out["value"] is None
    assert sent(out, "refresh") == []


@pytest.mark.parametrize(
    "reply",
    [UNREACHABLE, {"data": PAGE, "offline": True, "reason": "demo mode"}],
    ids=["unreachable", "demo"],
)
def test_an_unreachable_backend_or_demo_mode_never_sends_ingest(reply):
    out = content({"page": reply, "status": UNREACHABLE})
    assert sent(out, "ingest") == []


def test_a_partial_window_says_recent_edits_are_not_classified_in_banner_and_flags():
    out = content(
        {"page": page(risk_partial=True), "status": UNREACHABLE},
        "await open('Flags'); return [bannerText(), drawerText()];",
    )
    banner, drawer = out["value"]
    assert f"{FLAGGED} flagged so far (partial)" in banner
    first_flag = PAGE["flags"][0]["classification"]["rationale"]
    assert drawer.index("recent edits not yet classified") < drawer.index(first_flag)


def test_a_complete_window_says_flagged_without_partial():
    out = content({"page": page(), "status": UNREACHABLE}, "return bannerText();")
    assert f"{FLAGGED} flagged" in out["value"]
    assert "partial" not in out["value"]


@pytest.mark.parametrize(("errored", "shown"), [(0, False), (2, True)])
def test_the_banner_counts_edits_that_could_not_be_classified(errored, shown):
    out = content(
        {"page": page(risk_partial=True, classify_errored=errored), "status": UNREACHABLE},
        "return bannerText();",
    )
    assert ("could not be classified" in out["value"]) is shown
    assert ("2 could not be classified" in out["value"]) is shown


@pytest.mark.parametrize(
    ("reason", "text"),
    [
        ("budget", "classification stopped: Mubit call budget spent"),
        ("wikipedia", "classification stopped: Wikipedia request budget spent"),
    ],
)
def test_the_banner_shows_why_classification_stopped(reason, text):
    out = content(
        {"page": page(risk_partial=True, classify_stopped=reason), "status": UNREACHABLE},
        "return bannerText();",
    )
    assert text in out["value"]


@pytest.mark.parametrize(
    ("overrides", "shown"),
    [
        ({}, False),
        ({"risk_partial": True, "classify_open": 3}, True),
        ({"risk_partial": True, "classify_open": 0, "classify_errored": 4}, False),
        ({"risk_partial": True, "classify_open": 0, "classify_pending": 3}, False),
    ],
    ids=["classified", "open", "only-errored", "only-queued"],
)
def test_the_classify_button_and_its_cost_show_only_while_edits_are_open(overrides, shown):
    out = content(
        {"page": page(**overrides), "status": UNREACHABLE},
        "await open('Flags'); return [bannerText(), drawerText()];",
    )
    banner, drawer = out["value"]
    assert ("Classify recent edits" in drawer) is shown
    assert (COST in drawer) is shown
    assert sent(out, "classify") == []
    if overrides.get("classify_errored"):
        assert "4 could not be classified" in banner


def test_a_classify_click_sends_classify_once_and_shows_the_counts_and_progress():
    counts = {"queued": 5, "already": 2, "errored": 1, "pending": 0, "skipped": 3, "remaining": 280}
    replies = {
        "page": [page(risk_partial=True, classify_open=9), page(risk_partial=True, classify_pending=5)],
        "status": UNREACHABLE,
        "classify": {"data": counts, "offline": False, "status": 202},
    }
    out = content(replies, CLICK_CLASSIFY + "return bannerText();")
    assert sent(out, "classify") == [{"type": "classify", "title": TITLE}]
    assert (
        "5 queued, 2 already classified, 1 errors, 0 already queued, 3 not classified: Mubit budget, "
        "280 Mubit calls left"
    ) in out["value"]
    assert "classifying: 5 edits left" in out["value"]


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        (BUDGET_SPENT, "Mubit call budget for this session is spent"),
        (UNREACHABLE, "backend unreachable"),
    ],
)
def test_a_failed_classify_says_why_and_rereads_nothing(reply, reason):
    out = content(
        {"page": page(risk_partial=True, classify_open=3), "status": UNREACHABLE, "classify": reply},
        CLICK_CLASSIFY + "return drawerText();",
    )
    assert "classify failed: " + reason in out["value"]
    assert len(sent(out, "page")) == 1
    assert sent(out, "classify") == [{"type": "classify", "title": TITLE}]


def test_progress_rereads_every_ten_seconds_past_the_cache_and_stops_at_zero():
    replies = {
        "page": [
            page(risk_partial=True, classify_pending=2),
            page(risk_partial=True, classify_pending=1),
            page(classify_pending=0),
        ],
        "status": UNREACHABLE,
    }
    script = (
        "await open('Flags');"
        f"await tick({TEN_SECONDS - 1}); const before = bannerText();"
        "await tick(1); const mid = bannerText();"
        f"await tick({TEN_SECONDS}); await tick({60 * TEN_SECONDS});"
        "const shadow = host('edit-watch-drawer').shadow;"
        "return { before, mid, open: !find(shadow, 'aside').hidden, tab: find(shadow, 'button', 'Flags').className };"
    )
    out = content(replies, script)
    value = out["value"]
    assert sent(out, "page") == [
        {"type": "page", "title": TITLE},
        {"type": "page", "title": TITLE, "fresh": True},
        {"type": "page", "title": TITLE, "fresh": True},
    ]
    assert sent(out, "classify") == []
    assert "classifying: 2 edits left" in value["before"]
    assert "classifying: 1 edits left" in value["mid"]
    assert value["open"] is True
    assert "on" in value["tab"].split()


def test_the_footer_says_who_answered_each_step():
    status = {"data": STATUS, "offline": False, "status": 200}
    out = content({"page": page(), "status": status}, "return drawerText();")
    assert "extract: provider · classify: provider · brief: provider" in out["value"]
    assert "[object" not in out["value"]


def click_then_finish(status, read: str) -> dict:
    # A click queues 5 edits; the next read still has them pending, the poll after finds them done.
    counts = {"queued": 5, "already": 0, "errored": 0, "pending": 0, "skipped": 0, "remaining": 300}
    replies = {
        "page": [
            page(risk_partial=True, classify_open=5),
            page(risk_partial=True, classify_pending=5),
            page(classify_pending=0),
        ],
        "status": status,
        "classify": {"data": counts, "offline": False, "status": 202},
    }
    return content(
        replies, CLICK_CLASSIFY + f"const during = bannerText(); await tick({TEN_SECONDS});" + read
    )


def test_the_click_counts_leave_the_banner_once_the_queue_is_done():
    out = click_then_finish(UNREACHABLE, "return { during, after: bannerText() };")
    assert "5 queued" in out["value"]["during"]
    after = out["value"]["after"]
    assert "queued" not in after
    assert "Mubit calls left" not in after
    assert f"{FLAGGED} flagged" in after


def test_the_mubit_status_is_read_again_once_the_queue_is_done():
    spent = {**STATUS, "edit-classify": {**STATUS["edit-classify"], "requests": 999, "answered": 997}}
    status = [
        {"data": STATUS, "offline": False, "status": 200},
        {"data": spent, "offline": False, "status": 200},
    ]
    out = click_then_finish(
        status,
        "await click(find(host('edit-watch-drawer').shadow, 'button', 'Mubit')); return drawerText();",
    )
    assert "999 · 997" in out["value"]


def test_progress_stops_after_thirty_minutes():
    out = content(
        {"page": page(risk_partial=True, classify_pending=3), "status": UNREACHABLE},
        f"await tick({200 * TEN_SECONDS});",
    )
    assert len(sent(out, "page")) == 1 + 180


def test_progress_stops_when_the_page_unloads():
    out = content(
        {"page": page(risk_partial=True, classify_pending=3), "status": UNREACHABLE},
        f"await tick({TEN_SECONDS}); fire('pagehide'); await tick({60 * TEN_SECONDS});",
    )
    assert len(sent(out, "page")) == 2


def test_progress_stops_when_the_backend_stops_the_queue_and_says_why():
    replies = {
        "page": [
            page(risk_partial=True, classify_pending=3),
            page(risk_partial=True, classify_pending=2, classify_stopped="budget"),
        ],
        "status": UNREACHABLE,
    }
    out = content(replies, f"await tick({60 * TEN_SECONDS}); return bannerText();")
    assert len(sent(out, "page")) == 2
    assert "classification stopped: Mubit call budget spent" in out["value"]


def test_progress_does_not_start_on_a_page_already_stopped():
    out = content(
        {
            "page": page(risk_partial=True, classify_pending=3, classify_stopped="wikipedia"),
            "status": UNREACHABLE,
        },
        f"await tick({60 * TEN_SECONDS});",
    )
    assert len(sent(out, "page")) == 1


@pytest.mark.parametrize("failure", [UNREACHABLE, SERVER_ERROR], ids=["unreachable", "500"])
def test_progress_stops_when_a_read_fails(failure):
    replies = {"page": [page(risk_partial=True, classify_pending=3), failure, page(classify_pending=3)]}
    out = content({**replies, "status": UNREACHABLE}, f"await tick({60 * TEN_SECONDS});")
    assert len(sent(out, "page")) == 2
