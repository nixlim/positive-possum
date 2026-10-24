import math

from triage.features import (
    SAVE_TIME_TAGS,
    change_flags,
    edit_features,
    longest_run,
    markup_counts,
    parse_compare,
    summary_flags,
    summary_text,
    text_stats,
)
from triage.wiki import REVERTED_TAG

# A REST compare reply in the shape en.wikipedia.org/w/rest.php/v1/revision/{old}/compare/{new}
# returns: context, a removed line, an added line, and an in-place change whose highlight
# ranges are UTF-8 byte offsets (the "é" before them is two bytes).
LINE = "Café opened in 1990 2001."
COMPARE = {
    "from": {"id": 1, "slot_role": "main", "sections": []},
    "to": {"id": 2, "slot_role": "main", "sections": []},
    "diff": [
        {"type": 0, "lineNumber": 1, "text": "context", "offset": {"from": 0, "to": 0}},
        {"type": 2, "lineNumber": None, "text": "old line", "offset": {"from": 8, "to": None}},
        {
            "type": 1,
            "lineNumber": 2,
            "text": "LOL!!! <ref>x</ref> [[Foo]]",
            "offset": {"from": None, "to": 8},
        },
        {
            "type": 3,
            "lineNumber": 3,
            "text": LINE,
            "offset": {"from": 20, "to": 40},
            "highlightRanges": [
                {"start": LINE.encode().index(b"1990"), "length": 5, "type": 1},
                {"start": LINE.encode().index(b"2001"), "length": 4, "type": 0},
            ],
        },
    ],
}


def _rc(**kw):
    return {
        "revid": 2,
        "old_revid": 1,
        "timestamp": "2026-10-20T10:00:00Z",
        "user": "Someone",
        "userid": 7,
        "oldlen": 1000,
        "newlen": 1040,
        "comment": "/* Career */ fix",
        "tags": ["mobile edit", "possible vandalism", REVERTED_TAG],
        **kw,
    }


def test_parse_compare_slices_highlights_by_utf8_bytes():
    out = parse_compare(COMPARE)
    assert out["added"] == "LOL!!! <ref>x</ref> [[Foo]]\n2001"
    assert out["removed"] == "old line\n1990 "


def test_text_stats():
    s = text_stats("LOL!!! you are stupid aaaaaa кот")
    assert s["exclamations"] == 3
    assert s["lexicon_hits"] == 2  # LOL, stupid
    assert s["longest_run"] == 6
    assert 0 < s["upper_ratio"] < 0.2
    assert math.isclose(s["nonlatin_share"], 3 / 24)
    assert text_stats("")["upper_ratio"] == 0.0
    assert longest_run("") == 0


def test_markup_counts():
    added = '<ref name="a">x</ref> {{cite web}} [[Foo]] [[Category:Bar]] [[File:x.jpg]] https://e.org'
    m = markup_counts(added, "<ref>old</ref> [[Baz]]")
    assert m["refs_added"] == 1 and m["refs_removed"] == 1
    assert m["templates_added"] == 1
    assert m["wikilinks_added"] == 1 and m["wikilinks_removed"] == 1
    assert m["categories_added"] == 1
    assert m["images_added"] == 2  # the [[File: link and the .jpg name
    assert m["urls_added"] == 1


def test_change_flags():
    assert change_flags("a  b", "a b") == {"whitespace_only": 1.0, "digits_only": 0.0}
    assert change_flags("2001", "1990")["digits_only"] == 1.0
    assert change_flags("in 2001", "1990")["digits_only"] == 0.0
    assert change_flags("", "")["whitespace_only"] == 0.0


def test_summary_helpers():
    assert summary_text("/* Career */ fix typo") == "fix typo"
    assert summary_flags("/* Career */") == {
        "summary_present": 0.0,
        "summary_section_only": 1.0,
        "summary_len": 0.0,
    }
    assert summary_flags(None)["summary_present"] == 0.0
    assert summary_flags("ce")["summary_len"] == 2.0


def test_edit_features_never_reads_the_revert_label():
    assert REVERTED_TAG not in SAVE_TIME_TAGS
    user = {"editcount": 9, "registration": "2026-10-10T10:00:00Z"}
    f = edit_features(_rc(), parse_compare(COMPARE), user)
    assert not any("revert" in k for k in f)
    with_tag = edit_features(_rc(tags=["mobile edit", REVERTED_TAG]), parse_compare(COMPARE), user)
    without = edit_features(_rc(tags=["mobile edit"]), parse_compare(COMPARE), user)
    assert with_tag == without


def test_edit_features_values():
    user = {"editcount": 9, "registration": "2026-10-10T10:00:00Z"}
    f = edit_features(_rc(), parse_compare(COMPARE), user)
    assert f["editor_registered"] == 1.0 and f["editor_temporary"] == 0.0
    assert math.isclose(f["log_editcount"], math.log1p(9))
    assert math.isclose(f["log_account_age_days"], math.log1p(10))
    assert f["size_delta"] == 40 and f["abs_delta"] == 40
    assert f["tag_mobile_edit"] == 1.0 and f["tag_possible_vandalism"] == 1.0
    assert f["summary_present"] == 1.0
    assert f["added_words"] == 4


def test_edit_features_unknown_editor_and_no_summary_inputs():
    rc = _rc(user="~2026-1-1", temp=True)
    f = edit_features(rc, {"added": "", "removed": ""}, None, with_summary=False)
    assert f["editor_temporary"] == 1.0
    assert math.isnan(f["log_editcount"]) and math.isnan(f["log_account_age_days"])
    assert not any(k.startswith("summary") for k in f)
