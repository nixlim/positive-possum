import json

from triage import wiki
from triage.config import REPOS
from triage.wiki import diff_text, edit_body, is_content_edit, label_for, select_edits


def _rc(revid, tags=(), ts="2026-10-20T10:00:00Z", old=1, **kw):
    return {
        "revid": revid,
        "old_revid": old,
        "tags": list(tags),
        "timestamp": ts,
        "title": f"Page {revid}",
        "comment": "c",
        "userid": 5,
        "oldlen": 10,
        "newlen": 12,
        **kw,
    }


def test_label_for_reverted_tag():
    assert label_for(["visualeditor", "mw-reverted"]) == "revert"
    assert label_for(["visualeditor"]) == "keep"


def test_select_edits_balances_and_drops_reverting_edits():
    reverted = [
        _rc(10, ["mw-reverted"]),
        _rc(11, ["mw-reverted", "mw-undo"]),
        _rc(12, ["mw-reverted"]),
        _rc(13, ["mw-reverted"]),
    ]
    others = [_rc(20), _rc(21, ["mw-rollback"]), _rc(22, old=0), _rc(23, ["mw-reverted"]), _rc(24), _rc(25)]
    out = select_edits(reverted, others, 4)
    ids = {rc["revid"] for rc in out}
    assert ids == {10, 12, 20, 24}  # 11 undoes, 21 rolls back, 22 is a page creation, 23 is reverted
    assert sum(label_for(rc["tags"]) == "revert" for rc in out) == 2
    assert not is_content_edit(_rc(1, ["mw-manual-revert"]))


def test_diff_and_body_truncate():
    old = "a\nb\nc\nd"
    new = "a\nB\nc\nd"
    d = diff_text(old, new, 1000)
    assert d.startswith("@@") and "-b" in d and "+B" in d and "---" not in d
    assert diff_text("x", "x", 100) == "(no textual change)"
    body = edit_body(_rc(1, userid=0, anon=True, comment="fix"), old, new * 50, 40)
    assert body.startswith("Editor: anonymous (IP)\nEdit summary: fix\nSize change: +2 bytes")
    assert body.endswith("[truncated]")
    assert "registered account" in edit_body(_rc(1), old, new, 100)


def test_build_dataset_with_mocked_api(monkeypatch, tmp_path):
    cfg = REPOS["enwiki"]
    texts = {1: "a\nb", 2: "a\nVANDAL", 3: "a\nb", 4: "a\nb c"}

    def fake_api(client, host, params):
        if params.get("list") == "recentchanges":
            if params.get("rctag") == "mw-reverted":
                return {"query": {"recentchanges": [_rc(2, ["mw-reverted"], old=1)]}}
            return {
                "query": {"recentchanges": [_rc(4, old=3), _rc(2, ["mw-reverted"], old=1), _rc(9, old=8)]}
            }
        revs = [
            {"revid": r, "slots": {"main": {"content": texts[r]}}}
            for r in map(int, params["revids"].split("|"))
            if r in texts
        ]
        return {"query": {"pages": [{"revisions": revs}]}}

    monkeypatch.setattr(wiki, "_api", fake_api)
    monkeypatch.setattr(wiki, "_client", lambda: httpx_dummy())
    path = wiki.build_dataset(cfg, 4, data_dir=tmp_path)
    rows = [json.loads(lab) for lab in path.read_text().splitlines()]
    assert {r["number"]: r["labels"] for r in rows} == {2: ["revert"], 4: ["keep"]}  # 9: content missing
    assert "+VANDAL" in next(r for r in rows if r["number"] == 2)["body"]
    assert json.loads((tmp_path / "enwiki" / "labels.json").read_text()) == wiki.LABELS
    assert all(r["split"] in ("eval", "train") for r in rows)


class httpx_dummy:
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False
