"""The extension's service worker, run in node against a stubbed chrome API (tests/extension_harness.js)."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
HARNESS = Path(__file__).parent / "extension_harness.js"
KINDS = ["page", "brief", "status", "brief-generate", "refresh"]
TITLE = "Keir Starmer"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")


def worker(kind: str, backend, **settings) -> dict:
    scenario = {"settings": settings, "backend": backend, "message": {"type": kind, "title": TITLE}}
    r = subprocess.run(
        ["node", str(HARNESS), str(ROOT / "extension"), json.dumps(scenario)],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.parametrize("kind", KINDS)
def test_an_unreachable_backend_shows_no_fixture_data_on_a_real_article(kind):
    out = worker(kind, "down")
    assert out["requests"], "the worker should have tried the backend"
    reply = out["reply"]
    assert reply["data"] is None
    assert reply["offline"] is False
    assert reply["error"] == "backend unreachable"
    assert "(fixture)" not in json.dumps(reply)


def test_an_unreachable_backend_leaves_the_badge_grey():
    out = worker("page", "down")
    assert out["badge"] == {"color": "#75829a", "text": "–"}


def test_demo_mode_shows_the_fixtures_without_calling_the_backend():
    out = worker("page", "down", demo=True)
    assert out["requests"] == []
    assert out["reply"]["offline"] is True
    assert out["reply"]["data"]["title"].endswith("(fixture)")


def test_a_title_off_the_watchlist_gets_no_data_and_a_grey_badge():
    out = worker("page", {"status": 404, "body": {"detail": "not on the watchlist"}})
    assert out["reply"]["data"] is None
    assert out["reply"]["status"] == 404
    assert out["badge"]["text"] == "–"
