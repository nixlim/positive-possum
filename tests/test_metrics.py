from triage.metrics import agreement, latency_stats, score, score_by_family


def test_score_exact_and_f1():
    truth = {1: ["bug", "topic:gui"], 2: ["bug"], 3: ["crash", "topic:core"]}
    preds = {1: ["bug", "topic:gui"], 2: ["bug", "topic:editor"], 3: ["topic:core"]}
    m = score(preds, truth)
    assert m["n"] == 3
    assert m["exact"] == 1 / 3
    # tp = 2 + 1 + 1 = 4, fp = 1, fn = 1
    assert abs(m["precision"] - 4 / 5) < 1e-9
    assert abs(m["recall"] - 4 / 5) < 1e-9
    assert abs(m["f1"] - 0.8) < 1e-9


def test_score_ignores_issues_without_predictions():
    m = score({1: ["bug"]}, {1: ["bug"], 2: ["crash"]})
    assert m["n"] == 1 and m["exact"] == 1.0


def test_by_family_groups_on_prefix():
    truth = {1: ["bug", "topic:gui", "platform:macos"]}
    preds = {1: ["bug", "topic:core", "platform:macos"]}
    fam = score_by_family(preds, truth)
    assert fam["type"]["exact"] == 1.0
    assert fam["topic:"]["exact"] == 0.0
    assert fam["platform:"]["exact"] == 1.0


def test_agreement():
    a = {1: ["bug"], 2: ["bug", "crash"], 3: ["x"]}
    b = {1: ["bug"], 2: ["bug"], 4: ["y"]}
    m = agreement(a, b)
    assert m["n"] == 2
    assert m["exact"] == 0.5
    assert abs(m["jaccard"] - 0.75) < 1e-9


def test_nothing_scored_is_none_not_zero():
    assert score({}, {1: ["bug"]}) == {"n": 0, "exact": None, "precision": None, "recall": None, "f1": None}
    assert agreement({1: ["bug"]}, {2: ["bug"]}) == {"n": 0, "exact": None, "jaccard": None}
    assert agreement({}, {}) == {"n": 0, "exact": None, "jaccard": None}


def test_latency_stats():
    assert latency_stats([]) == {"p50_ms": None, "p90_ms": None}
    s = latency_stats(list(range(1, 11)))
    assert s["p50_ms"] == 5 and s["p90_ms"] == 10
