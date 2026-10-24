import json

import pytest

from triage import classification as cl
from triage import steps

EXTRACT = {
    "claims_added": ["Acme was fined"],
    "claims_removed": [],
    "negative_info": "added",
    "tone_shift": -1,
}
EMPTY = {"claims_added": [], "claims_removed": [], "negative_info": "none", "tone_shift": 0}
CLASSIFY = {
    "label": "content",
    "severity": 1,
    "review": False,
    "confidence": "high",
    "rationale": "Adds a fine.",
}
QUIET = {
    "size_delta": 45,
    "words_added": 10,
    "words_removed": 0,
    "refs_added": 0,
    "refs_removed": 0,
    "sections": ["History"],
    "words_removed_by_section": {},
}


def _ext(measured=None, extract=EXTRACT):
    return {**extract, "measured": {**QUIET, **(measured or {})}}


def _cls(**kw):
    return {**CLASSIFY, **kw, "extract_call_id": 1, "extract_served": "adapter-v1"}


def test_model_classification_unforced():
    assert cl.classification(_ext(), _cls()) == {
        **CLASSIFY,
        "review_model": False,
        "review_forced": None,
        "source": "model",
    }
    assert cl.classification(_ext(), _cls(review=True))["review"] is True


@pytest.mark.parametrize(
    ("measured", "rule"),
    [
        ({"refs_removed": 1}, "refs_removed"),
        (
            {"words_removed": 60, "words_added": 10, "words_removed_by_section": {"History": 60}},
            "net_words_removed",
        ),
        ({"words_removed": 3, "words_removed_by_section": {"Legal issues": 3}}, "controversy_words_removed"),
        (
            {"words_removed": 3, "words_removed_by_section": {"History": 2, "Criticism": 1}},
            "controversy_words_removed",
        ),
    ],
)
def test_each_forced_review_rule(measured, rule):
    out = cl.classification(_ext(measured), _cls())
    assert (out["review"], out["review_model"], out["review_forced"]) == (True, False, rule)


def test_rules_that_do_not_fire():
    near = {"words_removed": 59, "words_added": 10, "words_removed_by_section": {"History": 59}}
    zero_in_controversy = {"words_removed_by_section": {"Controversy": 0}}
    for measured in (near, zero_in_controversy):
        assert cl.classification(_ext(measured), _cls())["review_forced"] is None


def test_first_rule_wins():
    all_three = {
        "refs_removed": 2,
        "words_removed": 80,
        "words_added": 0,
        "words_removed_by_section": {"Controversies": 80},
    }
    assert cl.forced_review(all_three) == "refs_removed"
    assert cl.forced_review({**all_three, "refs_removed": 0}) == "net_words_removed"
    assert (
        cl.forced_review({**all_three, "refs_removed": 0, "words_removed": 10}) == "controversy_words_removed"
    )


def test_rule_label_and_not_classified_yet():
    tiny = _ext({"size_delta": -19}, EMPTY)
    assert cl.classification(tiny, None) == {
        **cl.RULE_LABEL,
        "review_model": False,
        "review_forced": None,
        "source": "rule",
    }
    forced = cl.classification(_ext({"size_delta": 5, "refs_removed": 1}, EMPTY), None)
    assert (forced["source"], forced["review"], forced["review_forced"]) == ("rule", True, "refs_removed")
    assert cl.classification(_ext({"size_delta": 20}, EMPTY), None) is None  # 20 bytes is not tiny
    assert cl.classification(_ext({"size_delta": 3}), None) is None  # tiny, but it has a claim


def test_extract_claims_cut_to_five_and_twenty_words():
    long = " ".join(f"w{i}" for i in range(30))
    reply = json.dumps({**EXTRACT, "claims_added": [long] + [str(i) for i in range(7)], "extra_key": 1})
    _, _, obj = cl.parse_extract(reply)
    assert obj["claims_added"] == [" ".join(f"w{i}" for i in range(20)), "0", "1", "2", "3"]
    assert "extra_key" not in obj


def test_rationale_cut_to_twenty_five_words():
    short = cl.parse_classify(json.dumps({**CLASSIFY, "rationale": "Adds  a\nfine."}))[2]
    assert short["rationale"] == "Adds  a\nfine."  # untouched when short enough
    long = cl.parse_classify(json.dumps({**CLASSIFY, "rationale": " ".join(["x"] * 40)}))
    assert long[0] == ["content"] and long[2]["rationale"] == " ".join(["x"] * 25)


def test_classify_rejects_bool_severity():
    for bad in ({**CLASSIFY, "severity": True}, {**CLASSIFY, "review": 1}, {"label": "content"}):
        with pytest.raises(ValueError):
            cl.parse_classify(json.dumps(bad))


def test_brief_closing_line_kept_once():
    kept = cl.parse_brief("text\n" + steps.CLOSING_LINE + "\n")[2]["brief_md"]
    assert kept == "text\n" + steps.CLOSING_LINE
