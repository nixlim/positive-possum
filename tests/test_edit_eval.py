import numpy as np
import pandas as pd
import pytest

from triage import edit_eval as ev


def _frame(seed: int = 0) -> pd.DataFrame:
    """A toy sample frame: reverted edits are anonymous and shout, kept ones add links."""
    rng = np.random.default_rng(seed)
    rows = []
    for i, split in enumerate(["train"] * 160 + ["test"] * 60 + ["live"] * 20):
        reverted = bool(i % 2)
        rows.append(
            {
                "revid": 1000 + i,
                "window": -1 if split == "live" else i // 40,
                "split": split,
                "kind": "anonymous" if reverted else "registered",
                "reverted": reverted,
                "review_reason": ("vandalism" if i % 4 == 1 else "none") if reverted else None,
                "declared": "none" if reverted else "formatting",
                "wiki_reason": "vandalism" if i % 4 == 1 else "copyedit" if not reverted else "other",
                "added": "LOL poop!!! " * 3 if reverted else "see [[Main page]] <ref>x</ref>",
                "removed": "",
                "weight": 1.0 if split == "live" else (1.0 if reverted else 9.0),
                "editor_temporary": 0.0,
                "editor_anonymous": float(reverted),
                "log_editcount": np.nan if reverted else 3 + rng.random(),
                "size_delta": rng.normal(),
                "summary_present": float(not reverted),
            }
        )
    return pd.DataFrame(rows)


def _call(revid: int, verdict: str, confidence: str = "high", reason: str = "vandalism") -> dict:
    return {
        "issue": str(revid),
        "labels": [verdict],
        "extra": {"reason": reason, "confidence": confidence},
        "latency_ms": 3000,
        "prompt_tokens": 1000,
        "completion_tokens": 30,
    }


def test_revert_model_scores_test_split_at_the_natural_rate():
    a = ev.revert_model(_frame())
    assert set(a.test["split"]) == {"test"}
    assert a.extra["odds_ratio"] == pytest.approx(1 / 9)  # 50% sampled vs 10% natural
    assert a.metrics.loc["blend", "base_rate"] == pytest.approx(0.1)
    assert a.metrics.loc["blend", "roc_auc"] == 1.0
    assert a.test["p_blend"].max() < 1.0


def test_sonnet_verdicts_take_the_first_answer_per_edit():
    calls = [_call(1, "revert"), _call(1, "keep"), {**_call(2, "keep"), "labels": []}, _call(3, "keep")]
    v = ev.sonnet_verdicts(calls)
    assert v["revid"].tolist() == [1, 3]
    assert v["sonnet_revert"].tolist() == [True, False]
    assert v["sonnet_usd"].iloc[0] == pytest.approx((1000 * 2.0 + 30 * 10.0) / 1e6)


def test_sonnet_comparison_matches_flag_rates_and_reports_cost():
    df = _frame()
    live = df[df["split"] == "live"]
    calls = [
        _call(r, "revert" if rev else "keep") for r, rev in zip(live["revid"], live["reverted"], strict=True)
    ]
    calls[0] = _call(live["revid"].iloc[0], "revert", "low")  # one false alarm
    s = ev.sonnet_comparison(ev.revert_model(df), df, calls)
    m = s.metrics
    assert list(m.index) == ["sonnet", "local"]
    assert m.loc["sonnet", "flag_rate"] == m.loc["local", "flag_rate"] == pytest.approx(11 / 20)
    assert m.loc["sonnet", "recall"] == 1.0 and m.loc["sonnet", "precision"] == pytest.approx(10 / 11)
    assert m.loc["local", "usd_per_1k_edits"] == 0.0
    assert m.loc["sonnet", "usd_per_1k_edits"] == pytest.approx(2.3)
    assert m.loc["sonnet", "median_ms_per_edit"] == 3000


def test_reason_agreement_uses_stated_reasons_and_declared_intents_only():
    live = pd.DataFrame(
        {
            "review_reason": ["vandalism", "none", None, None],
            "declared": ["none", "none", "formatting", "none"],
            "wiki_reason": ["vandalism", "other", "copyedit", "constructive"],
            "sonnet_reason": ["vandalism", "vandalism", "constructive", "constructive"],
        }
    )
    t = ev.reason_agreement(live)
    assert list(t.index) == ["copyedit", "vandalism"]
    assert t.loc["vandalism", "vandalism"] == 1 and t.loc["copyedit", "constructive"] == 1
    assert t.to_numpy().sum() == 2
