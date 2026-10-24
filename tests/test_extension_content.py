"""The extension's content scripts with no page data, run in node against a fake DOM (tests/content_harness.js)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
HARNESS = Path(__file__).parent / "content_harness.js"
UNREACHABLE = {
    "data": None,
    "offline": False,
    "error": "backend unreachable",
    "backend": "http://127.0.0.1:8322",
}
NOT_FOUND = {"data": None, "offline": False, "status": 404, "error": "HTTP 404"}
BUDGET_SPENT = {"data": None, "offline": False, "status": 429, "error": "HTTP 429"}
NOTICE = (
    "Edit watch: no backend at http://127.0.0.1:8322. Start it with uv run triage api, "
    "or turn on demo mode in Options."
)

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def content(replies: dict, script: str | None = None) -> dict:
    scenario = {"replies": replies, "script": script}
    r = subprocess.run(
        ["node", str(HARNESS), str(ROOT / "extension"), json.dumps(scenario)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def test_an_unreachable_backend_mounts_only_a_one_line_notice():
    out = content({"page": UNREACHABLE, "status": UNREACHABLE})
    assert out["hosts"] == [{"id": "edit-watch-notice", "text": NOTICE}]


def test_a_title_off_the_watchlist_shows_nothing_on_the_page():
    out = content({"page": NOT_FOUND, "status": UNREACHABLE})
    assert out["hosts"] == []


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
