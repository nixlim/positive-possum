"""The experiments behind notebooks/edit_intent.ipynb, on the table from
edit_analysis.sample_frame: (A) revert or keep, (B) the reviewer's stated reason for a revert,
(C) the editor's own declared intent for a kept edit, and (D) comparisons with Wikimedia's
Lift Wing revert-risk model and the live desk's logged Sonnet verdicts.

Train on the earlier windows, test on the later ones (a time split). Nothing here calls a
language model or the network; the comparisons read scores and verdicts already on disk."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.metrics import precision_recall_curve

from . import edit_model as em
from .edit_analysis import feature_columns
from .live import cost_usd
from .prompt import WIKI_REASONS

MIN_CLASS = 40  # a reason or intent needs this many examples to be modelled
CONFIDENCE = {"low": 1, "medium": 2, "high": 3}


@dataclass
class Result:
    """Metrics per model, the test rows with their scores, and anything else worth plotting."""

    metrics: pd.DataFrame
    test: pd.DataFrame
    extra: dict = field(default_factory=dict)


def _split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    return df[df["split"] == "train"], df[df["split"] == "test"]


def _ranked(y, scores: dict[str, pd.Series], w=None) -> pd.DataFrame:
    """binary_metrics per model, with a bootstrap interval on PR-AUC (small test sets)."""
    rows = {}
    for name, p in scores.items():
        lo, hi = em.pr_auc_interval(y, p, w)
        rows[name] = {**em.binary_metrics(y, p, w), "pr_auc_lo": lo, "pr_auc_hi": hi}
    return pd.DataFrame(rows).T


def _timed_proba(fitted: em.Fitted, X, text) -> tuple[dict, float]:
    t0 = time.perf_counter()
    proba = fitted.proba(X, text)
    return proba, (time.perf_counter() - t0) * 1000 / max(len(text), 1)


# ---- A: revert or keep ------------------------------------------------------------------


def revert_model(df: pd.DataFrame, seed: int = 0) -> Result:
    """Model A. Scores are prior-corrected to the natural revert rate; metrics are weighted
    to it. Rows: the three models, a one-rule baseline (editor not registered), and the
    tabular model without the editor's edit count (which is read after the save)."""
    corpus = df[df["split"] != "live"]
    train, test = _split(corpus)
    cols = feature_columns(df)
    fitted = em.fit(train[cols], train["added"].tolist(), train["reverted"], seed=seed)
    natural = (train["weight"] * train["reverted"]).sum() / train["weight"].sum()
    sample = train["reverted"].mean()
    odds_ratio = (natural / (1 - natural)) / (sample / (1 - sample))
    proba, ms = _timed_proba(fitted, test[cols], test["added"].tolist())
    test = test.copy()
    for name, p in proba.items():
        test[f"p_{name}"] = em.prior_correct(p[:, 1], odds_ratio)
    test["p_unregistered"] = test["editor_temporary"] + test["editor_anonymous"]
    no_count = [c for c in cols if c != "log_editcount"]
    ablated = em.tabular_model(False, seed).fit(train[no_count], train["reverted"])
    test["p_tabular_no_editcount"] = em.prior_correct(ablated.predict_proba(test[no_count])[:, 1], odds_ratio)
    names = [*em.MODELS, "tabular_no_editcount", "unregistered"]
    y, w = test["reverted"].astype(int), test["weight"]
    metrics = _ranked(y, {n: test[f"p_{n}"] for n in names}, w)
    extra = {
        "fitted": fitted,
        "columns": cols,
        "odds_ratio": odds_ratio,
        "ms_per_edit": ms,
        "calibration": {n: em.calibration(y, test[f"p_{n}"], w) for n in ("blend", "tabular")},
        "pr_curves": {n: precision_recall_curve(y, test[f"p_{n}"], sample_weight=w)[:2] for n in names},
    }
    return Result(metrics, test, extra)


# ---- B and C: reasons and intents ------------------------------------------------------


def _multiclass(rows: pd.DataFrame, label: str, cols: list[str], text: pd.Series, seed: int = 0) -> Result:
    counts = rows[label].value_counts()
    keep = counts[counts >= MIN_CLASS].index
    rows, text = rows[rows[label].isin(keep)], text[rows[label].isin(keep)]
    train, test = _split(rows)
    labels = sorted(keep)
    fitted = em.fit(train[cols], text[train.index].tolist(), train[label], balanced=True, seed=seed)
    preds = fitted.predict(test[cols], text[test.index].tolist())
    out = {name: em.multiclass_metrics(test[label], p, labels) for name, p in preds.items()}
    out["majority"] = em.majority_baseline(train[label], test[label], labels)
    metrics = pd.DataFrame(
        {n: {"n_test": m["n"], "accuracy": m["accuracy"], "macro_f1": m["macro_f1"]} for n, m in out.items()}
    ).T
    per_class = pd.DataFrame({n: m["per_class_f1"] for n, m in out.items()})
    per_class["n_train"] = train[label].value_counts()
    per_class["n_test"] = test[label].value_counts()
    test = test.assign(**{f"pred_{n}": p for n, p in preds.items()})
    return Result(metrics, test, {"per_class": per_class, "results": out, "counts": counts})


def reason_model(df: pd.DataFrame, seed: int = 0) -> Result:
    """Model B: the reviewer's stated reason for reverted edits that state one ("none" and
    the unclassified "other_stated" are left out). The editor's own summary is a fair input:
    it exists at save time."""
    rows = df[(df["split"] != "live") & df["reverted"]]
    rows = rows[~rows["review_reason"].isin(["none", "other_stated"]) & rows["review_reason"].notna()]
    return _multiclass(rows, "review_reason", feature_columns(df), rows["added"], seed)


def declared_model(df: pd.DataFrame, seed: int = 0) -> Result:
    """Model C: the editor's declared intent for kept edits that declare one, from the diff
    alone. The summary is the label, so no summary feature is an input; the text model reads
    both the added and the removed text."""
    rows = df[(df["split"] != "live") & ~df["reverted"]]
    rows = rows[~rows["declared"].isin(["none", "other"])]
    text = "+ " + rows["added"] + "\n- " + rows["removed"]
    return _multiclass(rows, "declared", feature_columns(df, with_summary=False), text, seed)


# ---- D: the comparisons -----------------------------------------------------------------


def liftwing_comparison(a: Result, liftwing: dict[int, dict]) -> Result:
    """Wikimedia's revertrisk-language-agnostic against the local models on the test edits
    it scored (weighted to the natural revert rate)."""
    test = a.test[a.test["revid"].isin([r for r, v in liftwing.items() if v.get("p") is not None])].copy()
    test["p_liftwing"] = test["revid"].map(lambda r: liftwing[r]["p"])
    y, w = test["reverted"].astype(int), test["weight"]
    names = ["liftwing", "blend", "tabular", "text"]
    metrics = _ranked(y, {n: test[f"p_{n}"] for n in names}, w)
    latency = [v["latency_ms"] for v in liftwing.values() if v.get("p") is not None]
    metrics["median_ms_per_edit"] = [float(np.median(latency))] + [a.extra["ms_per_edit"]] * 3
    return Result(metrics, test)


def sonnet_verdicts(calls: list[dict]) -> pd.DataFrame:
    """The first answered verdict per edit from the live desk's log."""
    rows = []
    for c in calls:
        if not c["labels"]:
            continue
        extra = c.get("extra") or {}
        rows.append(
            {
                "revid": int(c["issue"]),
                "sonnet_revert": "revert" in c["labels"],
                "sonnet_reason": extra.get("reason"),
                "sonnet_confidence": extra.get("confidence"),
                "sonnet_ms": c["latency_ms"],
                "sonnet_usd": cost_usd(c["prompt_tokens"], c["completion_tokens"]),
            }
        )
    return pd.DataFrame(rows).drop_duplicates("revid")


def sonnet_comparison(a: Result, df: pd.DataFrame, calls: list[dict]) -> Result:
    """Sonnet's logged keep/revert verdicts against the local model (trained on the corpus'
    train split) on the same live-desk edits. Truth is the mw-reverted tag at the snapshot.
    The local model flags its top-scored edits at Sonnet's own flag rate, for a like-for-like
    precision and recall; Sonnet's ranking score is its verdict times its confidence."""
    live = df[df["split"] == "live"].merge(sonnet_verdicts(calls), on="revid")
    fitted, cols = a.extra["fitted"], a.extra["columns"]
    proba, ms = _timed_proba(fitted, live[cols], live["added"].tolist())
    live["p_local"] = em.prior_correct(proba["blend"][:, 1], a.extra["odds_ratio"])
    sign = np.where(live["sonnet_revert"], 1, -1)
    live["sonnet_score"] = sign * live["sonnet_confidence"].map(CONFIDENCE).fillna(1)
    flagged = int(live["sonnet_revert"].sum())  # exactly as many as Sonnet, even with tied scores
    live["local_revert"] = live["p_local"].rank(method="first", ascending=False) <= flagged
    y = live["reverted"].astype(int)
    rank = _ranked(y, {"sonnet": live["sonnet_score"], "local": live["p_local"]})
    hard = {
        "sonnet": em.verdict_metrics(y, live["sonnet_revert"]),
        "local": em.verdict_metrics(y, live["local_revert"]),
    }
    metrics = pd.concat([pd.DataFrame(hard).T, rank[["pr_auc", "pr_auc_lo", "pr_auc_hi", "roc_auc"]]], axis=1)
    metrics["median_ms_per_edit"] = [live["sonnet_ms"].median(), ms]
    metrics["usd_per_1k_edits"] = [live["sonnet_usd"].mean() * 1000, 0.0]
    return Result(metrics, live, {"reasons": reason_agreement(live)})


def reason_agreement(live: pd.DataFrame) -> pd.DataFrame:
    """Sonnet's reason against the reference label mapped onto WIKI_REASONS: the reviewer's
    stated reason for reverted edits that give one, the editor's declared type for kept ones."""
    informative = live["review_reason"].notna() & ~live["review_reason"].isin(["none", "other_stated"])
    informative |= live["review_reason"].isna() & ~live["declared"].isin(["none", "other"])
    rows = live[informative]
    table = pd.crosstab(rows["wiki_reason"], rows["sonnet_reason"]).reindex(
        index=WIKI_REASONS, columns=WIKI_REASONS, fill_value=0
    )
    return table.loc[table.sum(axis=1) > 0]
