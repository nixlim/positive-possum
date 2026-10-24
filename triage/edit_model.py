"""Local edit classifiers and their metrics. No language model: a gradient-boosted model on
tabular features, a character n-gram logistic regression on the added text, and their blend.

The diff sample is case-control (every reverted edit, a small share of kept ones), so binary
metrics take per-row weights (1 / sampling fraction) that restore the natural revert rate,
and `prior_correct` maps scores trained on the sample back to natural-rate probabilities."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import (
    accuracy_score,
    average_precision_score,
    confusion_matrix,
    f1_score,
    precision_recall_curve,
    roc_auc_score,
)
from sklearn.pipeline import Pipeline, make_pipeline

MODELS = ("tabular", "text", "blend")


def tabular_model(balanced: bool, seed: int = 0) -> HistGradientBoostingClassifier:
    return HistGradientBoostingClassifier(
        max_iter=300,
        learning_rate=0.05,
        max_leaf_nodes=31,
        l2_regularization=1.0,
        class_weight="balanced" if balanced else None,
        random_state=seed,
    )


def text_model(balanced: bool) -> Pipeline:
    """Character 2-5-grams within word boundaries, case kept (shouting is a signal)."""
    return make_pipeline(
        TfidfVectorizer(
            analyzer="char_wb",
            ngram_range=(2, 5),
            lowercase=False,
            min_df=3,
            max_features=200_000,
            sublinear_tf=True,
        ),
        LogisticRegression(C=2.0, max_iter=3000, class_weight="balanced" if balanced else None),
    )


@dataclass
class Fitted:
    tabular: HistGradientBoostingClassifier
    text: Pipeline
    classes: np.ndarray

    def proba(self, X, text: list[str]) -> dict[str, np.ndarray]:
        """Class probabilities from each model and their mean, columns in `classes` order."""
        tab = self.tabular.predict_proba(X)
        txt = self.text.predict_proba(text)
        return {"tabular": tab, "text": txt, "blend": (tab + txt) / 2}

    def predict(self, X, text: list[str]) -> dict[str, np.ndarray]:
        return {k: self.classes[p.argmax(1)] for k, p in self.proba(X, text).items()}


def fit(X, text: list[str], y, balanced: bool = False, seed: int = 0) -> Fitted:
    """Train both models. `balanced` weights classes equally (for macro F1 on rare classes)."""
    tab = tabular_model(balanced, seed).fit(X, y)
    txt = text_model(balanced).fit(text, y)
    assert list(tab.classes_) == list(txt.classes_)
    return Fitted(tab, txt, tab.classes_)


# ---- binary: revert or keep -----------------------------------------------------------


def prior_correct(p: np.ndarray, odds_ratio: float) -> np.ndarray:
    """Probabilities from a model trained on a resampled set, mapped to the natural base rate.
    `odds_ratio` = (natural odds of a revert) / (odds in the training sample)."""
    p = np.clip(p, 1e-9, 1 - 1e-9)
    odds = p / (1 - p) * odds_ratio
    return odds / (1 + odds)


def precision_at_recall(y, p, w, recall: float) -> float:
    prec, rec, _ = precision_recall_curve(y, p, sample_weight=w)
    return float(prec[rec >= recall].max())


def recall_at_precision(y, p, w, precision: float) -> float:
    prec, rec, _ = precision_recall_curve(y, p, sample_weight=w)
    ok = rec[prec >= precision]
    return float(ok.max()) if ok.size else 0.0


def binary_metrics(y, p, w=None) -> dict[str, float]:
    """Ranking quality of revert scores `p` for labels `y` (1 = reverted), weighted to the
    natural revert rate by `w`."""
    y, p = np.asarray(y), np.asarray(p)
    w = np.ones(len(y)) if w is None else np.asarray(w, dtype=float)
    return {
        "n": len(y),
        "n_reverted": int(y.sum()),
        "base_rate": float((w * y).sum() / w.sum()),
        "pr_auc": float(average_precision_score(y, p, sample_weight=w)),
        "roc_auc": float(roc_auc_score(y, p, sample_weight=w)),
        "precision_at_recall_0.5": precision_at_recall(y, p, w, 0.5),
        "recall_at_precision_0.9": recall_at_precision(y, p, w, 0.9),
    }


def pr_auc_interval(y, p, w=None, n_boot: int = 500, seed: int = 0) -> tuple[float, float]:
    """95% percentile-bootstrap interval of the weighted PR-AUC, resampling edits. The same
    seed draws the same resamples for every model scored on the same edits."""
    y, p = np.asarray(y), np.asarray(p)
    w = np.ones(len(y)) if w is None else np.asarray(w, dtype=float)
    rng = np.random.default_rng(seed)
    stats = []
    for _ in range(n_boot):
        i = rng.integers(0, len(y), len(y))
        if 0 < y[i].sum() < len(i):
            stats.append(average_precision_score(y[i], p[i], sample_weight=w[i]))
    lo, hi = np.quantile(stats, [0.025, 0.975])
    return float(lo), float(hi)


def verdict_metrics(y, flagged, w=None) -> dict[str, float]:
    """Precision, recall and flag rate of hard revert verdicts (for Sonnet's keep/revert)."""
    y, f = np.asarray(y, dtype=bool), np.asarray(flagged, dtype=bool)
    w = np.ones(len(y)) if w is None else np.asarray(w, dtype=float)
    tp, flagged_w, pos_w = (w * (y & f)).sum(), (w * f).sum(), (w * y).sum()
    return {
        "n": len(y),
        "n_reverted": int(y.sum()),
        "flag_rate": float(flagged_w / w.sum()),
        "precision": float(tp / flagged_w) if flagged_w else float("nan"),
        "recall": float(tp / pos_w) if pos_w else float("nan"),
    }


def calibration(y, p, w=None, bins: int = 10) -> dict[str, np.ndarray]:
    """Weighted mean predicted probability against the weighted observed revert rate, in
    quantile bins of `p`."""
    y, p = np.asarray(y, dtype=float), np.asarray(p, dtype=float)
    w = np.ones(len(y)) if w is None else np.asarray(w, dtype=float)
    edges = np.unique(np.quantile(p, np.linspace(0, 1, bins + 1)))
    idx = np.clip(np.searchsorted(edges, p, side="right") - 1, 0, len(edges) - 2)
    pred, obs, mass = [], [], []
    for b in range(len(edges) - 1):
        m = idx == b
        if m.any():
            pred.append(np.average(p[m], weights=w[m]))
            obs.append(np.average(y[m], weights=w[m]))
            mass.append(w[m].sum() / w.sum())
    return {"predicted": np.array(pred), "observed": np.array(obs), "share": np.array(mass)}


# ---- multi-class: reviewer reason, declared intent -----------------------------------


def multiclass_metrics(y_true, y_pred, labels) -> dict:
    labels = list(labels)
    per = f1_score(y_true, y_pred, labels=labels, average=None, zero_division=0)
    return {
        "n": len(y_true),
        "accuracy": float(accuracy_score(y_true, y_pred)),
        "macro_f1": float(per.mean()),
        "per_class_f1": dict(zip(labels, per.round(3).tolist(), strict=True)),
        "confusion": confusion_matrix(y_true, y_pred, labels=labels),
        "labels": labels,
    }


def majority_baseline(y_train, y_test, labels) -> dict:
    """Always predict the most common training class."""
    values, counts = np.unique(np.asarray(y_train), return_counts=True)
    top = values[counts.argmax()]
    return multiclass_metrics(y_test, np.full(len(y_test), top, dtype=object), labels)
