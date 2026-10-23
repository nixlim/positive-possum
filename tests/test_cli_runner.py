import pytest

from triage import cli, runner
from triage.config import repo
from triage.llm import CallResult
from triage.store import Store

LABELS = [{"name": "bug", "description": ""}, {"name": "crash", "description": ""}]
ROWS = [
    {"number": 1, "title": "Crash on start", "body": "", "labels": ["crash"], "split": "eval"},
    {"number": 2, "title": "Wrong color", "body": "", "labels": ["bug"], "split": "train"},
]


def test_correct_refuses_eval_issue(monkeypatch):
    monkeypatch.setattr(cli, "load_dataset", lambda cfg: (LABELS, ROWS))

    def no_write(*a, **k):
        raise AssertionError("eval labels written to guidelines")

    monkeypatch.setattr(cli.gl, "add_correction", no_write)
    with pytest.raises(SystemExit, match="eval split"):
        cli.main(["correct", "--repo", "godot", "--issue", "1", "--labels", "crash"])


def test_watch_defaults_to_pulled_repos_and_survives_failures(monkeypatch, tmp_path, capsys):
    (tmp_path / "godot").mkdir()
    (tmp_path / "godot" / "issues.jsonl").write_text("")
    monkeypatch.setattr(cli, "DATA_DIR", tmp_path)
    assert cli.repos_with_dataset() == ["godot"]

    calls = []

    def fake_run(cfg, *a, **k):
        calls.append(cfg.key)
        if cfg.key == "flutter":
            raise FileNotFoundError("no dataset")
        if calls.count("godot") == 1:
            raise SystemExit("no eval issues")
        return {"run_id": "r", "n": 1, "errors": 0, "exact": 1.0, "f1": 1.0, "p50_ms": 5, "served": {}}

    rounds = []

    def fake_sleep(s):
        rounds.append(s)
        if len(rounds) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli, "Store", lambda: None)
    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["watch", "--repos", "flutter", "godot", "--every", "1"])
    assert calls == ["flutter", "godot", "flutter", "godot"]
    out = capsys.readouterr()
    assert "round failed" in out.err and "r: n=1 errors=0 exact=100%" in out.out


def test_watch_prints_dash_for_unscored_round(monkeypatch, capsys):
    def fake_run(cfg, *a, **k):
        return {"run_id": "r", "n": 0, "errors": 2, "exact": None, "f1": None, "p50_ms": None, "served": {},
                "agreement_vs_direct": {"n": 0, "exact": None, "jaccard": None}}

    def stop(s):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli, "Store", lambda: None)
    monkeypatch.setattr(cli.time, "sleep", stop)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["watch", "--repos", "godot"])
    assert "r: n=0 errors=2 exact=- f1=- agree_vs_direct=-" in capsys.readouterr().out


def test_agreement_only_against_same_guidelines_and_model(monkeypatch, tmp_path):
    guide = {"text": "- rule A"}
    monkeypatch.setattr(runner, "load_dataset", lambda cfg: (LABELS, ROWS))
    monkeypatch.setattr(runner, "client_for", lambda route: None)

    def load(key, eval_issues):
        assert eval_issues == {1}  # eval issues' correction lines are filtered out of prompts
        return guide["text"]

    monkeypatch.setattr(runner.gl, "load", load)
    monkeypatch.setattr(runner, "classify", lambda client, route, *a, model: CallResult(
        route, ["crash"], model, model, None, 5, None, None))
    store = Store(tmp_path / "t.sqlite")
    cfg = repo("godot")

    direct = runner.run(cfg, "direct", "eval", store, verbose=False)
    m = runner.run(cfg, "mubit", "eval", store, verbose=False)
    assert m["baseline_run"] == direct["run_id"] and m["agreement_vs_direct"]["exact"] == 1.0
    assert m["guidelines_sha"] == direct["guidelines_sha"] and m["model"] == direct["model"]

    guide["text"] = "- rule A\n- rule B"
    m = runner.run(cfg, "mubit", "eval", store, verbose=False)
    assert "agreement_vs_direct" not in m and "baseline_run" not in m and "baseline_mismatch" in m

    guide["text"] = "- rule A"
    m = runner.run(cfg, "mubit", "eval", store, verbose=False, model="gpt-other")
    assert "agreement_vs_direct" not in m and "baseline_mismatch" in m

    # A re-pull that changes the label set changes the system prompt and schema: a new task.
    relabelled = LABELS + [{"name": "regression", "description": ""}]
    monkeypatch.setattr(runner, "load_dataset", lambda cfg: (relabelled, ROWS))
    m = runner.run(cfg, "mubit", "eval", store, verbose=False)
    assert m["task_sha"] != direct["task_sha"]
    assert "agreement_vs_direct" not in m and "baseline_run" not in m and "baseline_mismatch" in m


EVAL_ROWS = [
    {"number": n, "title": f"Issue {n}", "body": "", "labels": ["crash"], "split": "eval"} for n in (1, 3, 5)
] + [{"number": 2, "title": "Wrong color", "body": "", "labels": ["bug"], "split": "train"}]


def _runner_with(monkeypatch, fail):
    """Runner on EVAL_ROWS; `fail(route, issue_number)` decides which calls error."""
    monkeypatch.setattr(runner, "load_dataset", lambda cfg: (LABELS, EVAL_ROWS))
    monkeypatch.setattr(runner, "client_for", lambda route: None)
    monkeypatch.setattr(runner.gl, "load", lambda key, eval_issues: "")

    def classify(client, route, full_name, labels, issue, guide, model):
        if fail(route, issue["number"]):
            return CallResult(route, [], model, None, f"mreq_{issue['number']}", 7, None, None,
                              {"x-middleware-request-id": f"mreq_{issue['number']}"}, {"status_code": 429},
                              "HTTP 429: rate limited")
        return CallResult(route, ["crash"], model, model, f"mreq_{issue['number']}", 5, None, None)

    monkeypatch.setattr(runner, "classify", classify)


def test_errored_calls_are_logged_but_not_scored(monkeypatch, tmp_path):
    _runner_with(monkeypatch, lambda route, n: n == 3)
    store = Store(tmp_path / "t.sqlite")
    m = runner.run(repo("godot"), "mubit", "eval", store, verbose=False)
    rows = store.db.execute("SELECT issue, request_id, headers, error FROM calls WHERE run_id=? ORDER BY issue",
                            (m["run_id"],)).fetchall()
    assert [r[0] for r in rows] == [1, 3, 5]
    _, rid, headers, err = rows[1]
    assert rid == "mreq_3" and "mreq_3" in headers and err.startswith("HTTP 429")
    assert m["n"] == 2 and m["errors"] == 1 and m["exact"] == 1.0
    run = store.runs("godot")[0]
    assert run["n"] == 2 and run["errors"] == 1 and run["calls"] == 3


def test_all_errored_run_reports_none_not_zero(monkeypatch, tmp_path):
    from triage.report import render

    _runner_with(monkeypatch, lambda route, n: route == "direct")
    store = Store(tmp_path / "t.sqlite")
    direct = runner.run(repo("godot"), "direct", "eval", store, verbose=False)
    assert direct["n"] == 0 and direct["errors"] == 3
    assert direct["exact"] is None and direct["f1"] is None and direct["p50_ms"] is None
    # A direct baseline with nothing scored is no baseline: no agreement of 0.
    m = runner.run(repo("godot"), "mubit", "eval", store, verbose=False)
    assert m["n"] == 3 and "agreement_vs_direct" not in m and "baseline_mismatch" in m
    direct_line = next(l for l in render(store, "godot").splitlines() if " direct " in l)
    assert direct_line.split()[3:8] == ["0", "3", "-", "-", "-"]


def test_replay_is_not_compared_with_the_eval_baseline(monkeypatch, tmp_path):
    _runner_with(monkeypatch, lambda route, n: False)
    store = Store(tmp_path / "t.sqlite")
    runner.run(repo("godot"), "direct", "eval", store, verbose=False)
    m = runner.run(repo("godot"), "mubit", "replay", store, verbose=False)
    assert m["n"] == 1 and "agreement_vs_direct" not in m and "baseline_mismatch" not in m
