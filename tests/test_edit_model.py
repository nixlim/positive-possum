import numpy as np
import pandas as pd
import pytest

from triage import edit_model as em


def test_prior_correct_maps_sample_odds_to_natural_odds():
    assert em.prior_correct(np.array([0.5]), 1 / 9)[0] == pytest.approx(0.1)
    assert em.prior_correct(np.array([0.2]), 1.0)[0] == pytest.approx(0.2)


def test_binary_metrics_weighting():
    y = np.array([1, 1, 0, 0, 0, 0])
    p = np.array([0.9, 0.8, 0.7, 0.1, 0.1, 0.1])
    plain = em.binary_metrics(y, p)
    assert plain["roc_auc"] == 1.0 and plain["pr_auc"] == 1.0
    assert plain["base_rate"] == pytest.approx(1 / 3)
    w = np.array([1, 1, 10, 10, 10, 10])
    weighted = em.binary_metrics(y, p, w)
    assert weighted["base_rate"] == pytest.approx(2 / 42)
    assert weighted["recall_at_precision_0.9"] == 1.0


def test_precision_and_recall_at_operating_points():
    y = np.array([1, 0, 1, 0])
    p = np.array([0.9, 0.8, 0.7, 0.1])
    w = np.ones(4)
    assert em.precision_at_recall(y, p, w, 0.5) == 1.0
    assert em.recall_at_precision(y, p, w, 0.9) == 0.5


def test_verdict_metrics():
    m = em.verdict_metrics([1, 1, 0, 0], [1, 0, 1, 0])
    assert m == {"n": 4, "n_reverted": 2, "flag_rate": 0.5, "precision": 0.5, "recall": 0.5}
    assert np.isnan(em.verdict_metrics([0, 0], [0, 0])["precision"])


def test_calibration_bins():
    rng = np.random.default_rng(0)
    p = rng.uniform(size=2000)
    y = rng.uniform(size=2000) < p
    cal = em.calibration(y, p, bins=5)
    assert len(cal["predicted"]) == 5
    assert cal["share"].sum() == pytest.approx(1.0)
    assert np.abs(cal["predicted"] - cal["observed"]).max() < 0.06


def test_multiclass_metrics_and_majority_baseline():
    labels = ["a", "b", "c"]
    m = em.multiclass_metrics(["a", "b", "c", "a"], ["a", "b", "a", "a"], labels)
    assert m["accuracy"] == 0.75
    assert m["per_class_f1"]["c"] == 0.0
    assert m["confusion"].sum() == 4
    base = em.majority_baseline(["a", "a", "b"], ["a", "b", "c"], labels)
    assert base["accuracy"] == pytest.approx(1 / 3)
    assert base["macro_f1"] == pytest.approx(0.5 / 3)  # F1 of "a" is 0.5, the others 0


def test_fit_learns_a_separable_toy_problem():
    rng = np.random.default_rng(0)
    n = 120
    y = np.array(["vandal", "good"] * (n // 2))
    X = pd.DataFrame(
        {"x": np.where(y == "vandal", 1.0, 0.0) + rng.normal(0, 0.1, n), "z": rng.normal(size=n)}
    )
    text = ["LOL POOP!!!" if v == "vandal" else "added a citation to the journal" for v in y]
    fitted = em.fit(X, text, y)
    preds = fitted.predict(X, text)
    assert set(preds) == set(em.MODELS)
    assert all((p == y).mean() > 0.95 for p in preds.values())
    proba = fitted.proba(X, text)["blend"]
    assert proba.shape == (n, 2) and np.allclose(proba.sum(1), 1)


def test_pr_auc_interval_brackets_the_estimate_and_is_reproducible():
    rng = np.random.default_rng(1)
    y = rng.random(300) < 0.2
    p = y * 0.3 + rng.random(300)
    lo, hi = em.pr_auc_interval(y, p)
    assert lo < em.binary_metrics(y, p)["pr_auc"] < hi
    assert em.pr_auc_interval(y, p) == (lo, hi)
