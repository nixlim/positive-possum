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
                "agreement_vs_reference": {"n": 0, "exact": None, "jaccard": None}}

    def stop(s):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "run", fake_run)
    monkeypatch.setattr(cli, "Store", lambda: None)
    monkeypatch.setattr(cli.time, "sleep", stop)
    with pytest.raises(KeyboardInterrupt):
        cli.main(["watch", "--repos", "godot"])
    assert "r: n=0 errors=2 exact=- f1=- agree_vs_ref=-" in capsys.readouterr().out


def test_reference_is_the_first_run_with_same_task_guidelines_and_model(monkeypatch, tmp_path):
    guide = {"text": "- rule A"}
    monkeypatch.setattr(runner, "load_dataset", lambda cfg: (LABELS, ROWS))
    monkeypatch.setattr(runner, "client", lambda: None)

    def load(key, eval_issues):
        assert eval_issues == {1}  # eval issues' correction lines are filtered out of prompts
        return guide["text"]

    monkeypatch.setattr(runner.gl, "load", load)
    monkeypatch.setattr(runner, "classify", lambda c, *a, model: CallResult(["crash"], model, model, None, 5, None, None))
    store = Store(tmp_path / "t.sqlite")
    cfg = repo("godot")

    first = runner.run(cfg, "eval", store, verbose=False)
    assert "reference_run" not in first and "reference_mismatch" not in first
    second = runner.run(cfg, "eval", store, verbose=False)
    third = runner.run(cfg, "eval", store, verbose=False)
    assert second["reference_run"] == third["reference_run"] == first["run_id"]
    assert third["agreement_vs_reference"]["exact"] == 1.0
    assert third["guidelines_sha"] == first["guidelines_sha"] and third["model"] == first["model"]

    guide["text"] = "- rule A\n- rule B"
    m = runner.run(cfg, "eval", store, verbose=False)
    assert "agreement_vs_reference" not in m and "reference_run" not in m and "reference_mismatch" in m

    guide["text"] = "- rule A"
    m = runner.run(cfg, "eval", store, verbose=False, model="claude-other")
    assert "agreement_vs_reference" not in m and "reference_mismatch" in m

    # A re-pull that changes the label set changes the system prompt and schema: a new task.
    relabelled = LABELS + [{"name": "regression", "description": ""}]
    monkeypatch.setattr(runner, "load_dataset", lambda cfg: (relabelled, ROWS))
    m = runner.run(cfg, "eval", store, verbose=False)
    assert m["task_sha"] != first["task_sha"]
    assert "agreement_vs_reference" not in m and "reference_run" not in m and "reference_mismatch" in m


EVAL_ROWS = [
    {"number": n, "title": f"Issue {n}", "body": "", "labels": ["crash"], "split": "eval"} for n in (1, 3, 5)
] + [{"number": 2, "title": "Wrong color", "body": "", "labels": ["bug"], "split": "train"}]


def test_reference_must_be_served_by_the_provider(monkeypatch, tmp_path):
    # Guidelines change without forking the Mubit task, so the first run with new guidelines
    # can already be served by the adapter. It must not become the provider reference.
    monkeypatch.setattr(runner, "load_dataset", lambda cfg: (LABELS, EVAL_ROWS))
    monkeypatch.setattr(runner, "client", lambda: None)
    monkeypatch.setattr(runner.gl, "load", lambda key, eval_issues: "- rule A")
    served = {"by": "adapter-v1"}
    monkeypatch.setattr(runner, "classify", lambda c, *a, model: CallResult(
        ["crash"], model, served["by"], None, 5, None, None))
    store = Store(tmp_path / "t.sqlite")
    cfg, model = repo("godot"), "claude-sonnet-5-5"

    adapter = runner.run(cfg, "eval", store, verbose=False, model=model)
    assert adapter["served"] == {"adapter-v1": 3}
    m = runner.run(cfg, "eval", store, verbose=False, model=model)
    assert "reference_run" not in m and "agreement_vs_reference" not in m
    assert adapter["run_id"] in m["reference_mismatch"] and "adapter-v1" in m["reference_mismatch"]

    served["by"] = "claude-sonnet-5-5-20261001"  # the provider, with a date suffix
    provider = runner.run(cfg, "eval", store, verbose=False, model=model)
    m = runner.run(cfg, "eval", store, verbose=False, model=model)
    assert m["reference_run"] == provider["run_id"] != adapter["run_id"]
    assert m["reference_served"] == {"claude-sonnet-5-5-20261001": 3}


def test_served_by_provider():
    model = "claude-sonnet-5-5"
    assert runner.served_by_provider({model: 2, "?": 1}, model)  # '?' = no reply, says nothing
    assert runner.served_by_provider({model + "-20261001": 2}, model)
    assert not runner.served_by_provider({model: 2, "adapter-v1": 1}, model)
    assert not runner.served_by_provider({model + "-adapter": 2}, model)
    assert not runner.served_by_provider({"?": 3}, model) and not runner.served_by_provider({}, model)


def _runner_with(monkeypatch, fail):
    """Runner on EVAL_ROWS; `fail(issue_number, call_index)` decides which calls error."""
    monkeypatch.setattr(runner, "load_dataset", lambda cfg: (LABELS, EVAL_ROWS))
    monkeypatch.setattr(runner, "client", lambda: None)
    monkeypatch.setattr(runner.gl, "load", lambda key, eval_issues: "")
    count = {"n": 0}

    def classify(c, full_name, labels, issue, guide, model):
        count["n"] += 1
        if fail(issue["number"], count["n"]):
            return CallResult([], model, None, f"mreq_{issue['number']}", 7, None, None,
                              {"x-middleware-request-id": f"mreq_{issue['number']}"}, {"status_code": 429},
                              "HTTP 429: rate limited")
        return CallResult(["crash"], model, model, f"mreq_{issue['number']}", 5, None, None)

    monkeypatch.setattr(runner, "classify", classify)


def test_errored_calls_are_logged_but_not_scored(monkeypatch, tmp_path):
    _runner_with(monkeypatch, lambda n, i: n == 3)
    store = Store(tmp_path / "t.sqlite")
    m = runner.run(repo("godot"), "eval", store, verbose=False)
    rows = store.db.execute("SELECT issue, request_id, headers, error FROM calls WHERE run_id=? ORDER BY issue",
                            (m["run_id"],)).fetchall()
    assert [r[0] for r in rows] == [1, 3, 5]
    _, rid, headers, err = rows[1]
    assert rid == "mreq_3" and "mreq_3" in headers and err.startswith("HTTP 429")
    assert m["n"] == 2 and m["errors"] == 1 and m["exact"] == 1.0
    run = store.runs("godot")[0]
    assert run["n"] == 2 and run["errors"] == 1 and run["calls"] == 3


def test_all_errored_run_is_neither_scored_nor_a_reference(monkeypatch, tmp_path):
    from triage.report import render

    _runner_with(monkeypatch, lambda n, i: i <= 3)  # the whole first run fails
    store = Store(tmp_path / "t.sqlite")
    first = runner.run(repo("godot"), "eval", store, verbose=False)
    assert first["n"] == 0 and first["errors"] == 3
    assert first["exact"] is None and first["f1"] is None and first["p50_ms"] is None
    # A run with nothing scored is no reference: the next scored run becomes it.
    second = runner.run(repo("godot"), "eval", store, verbose=False)
    assert second["n"] == 3 and "agreement_vs_reference" not in second and "reference_mismatch" in second
    third = runner.run(repo("godot"), "eval", store, verbose=False)
    assert third["reference_run"] == second["run_id"] and third["agreement_vs_reference"]["exact"] == 1.0
    first_line = render(store, "godot").splitlines()[1]
    assert first_line.split()[3:8] == ["0", "3", "-", "-", "-"]


def test_replay_is_not_compared_with_the_eval_reference(monkeypatch, tmp_path):
    _runner_with(monkeypatch, lambda n, i: False)
    store = Store(tmp_path / "t.sqlite")
    runner.run(repo("godot"), "eval", store, verbose=False)
    m = runner.run(repo("godot"), "replay", store, verbose=False)
    assert m["n"] == 1 and "agreement_vs_reference" not in m and "reference_mismatch" not in m
