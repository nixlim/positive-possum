from triage import guidelines as gl
from triage.llm import CallResult, parse_labels
from triage.store import Store


def _res(labels, served="claude-sonnet-5-5", error=None):
    return CallResult(labels, "claude-sonnet-5-5", served, "mreq_x", 123, 10, 5, {"h": "v"}, {"ok": 1}, error)


def test_store_roundtrip(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.log_call("run1", "eval", "godot", 1, _res(["bug"]), truth=["bug"])
    s.log_call("run1", "eval", "godot", 2, _res([], error="boom"))
    s.log_call("run1", "eval", "godot", 3, _res(["crash"], served="adapter-v1"))
    assert s.predictions("run1") == {1: ["bug"], 3: ["crash"]}
    assert s.served_models("run1") == {"claude-sonnet-5-5": 2, "adapter-v1": 1}
    s.log_run("run1", "eval", "mubit", "godot", 3, 1, {"exact": 0.5})
    assert s.runs("godot")[0]["exact"] == 0.5
    assert s.latest_prediction("godot", 3) == ["crash"]


def test_latest_prediction_ignores_correct_resends(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    s.log_call("rep", "replay", "godot", 7, _res(["bug"]))
    s.log_call("correct-godot-7", "correct", "godot", 7, _res(["crash"]))
    assert s.latest_prediction("godot", 7) == ["bug"]


def test_answered_latest_or_first_skipping_kinds(tmp_path):
    s = Store(tmp_path / "t.sqlite")
    ids = [
        s.log_call("p0", "probe", "edit-extract", 7, _res([], served="probe-0")),
        s.log_call("r1", "pipeline", "edit-extract", 7, _res([], error="boom")),
        s.log_call("r2", "pipeline", "edit-extract", 7, _res([], served="first")),
        s.log_call("r3", "live", "edit-extract", 7, _res([], served="later")),
        s.log_call("p4", "probe", "edit-extract", 7, _res([], served="probe-4")),
    ]
    assert ids == sorted(ids) and len(set(ids)) == 5
    assert s.answered("edit-extract")[7]["model_served"] == "probe-4"
    latest = s.answered("edit-extract", skip_kinds=("probe",))[7]
    assert (latest["id"], latest["model_served"]) == (ids[3], "later")
    first = s.answered("edit-extract", skip_kinds=("probe",), first=True)[7]
    assert (first["id"], first["model_served"], first["extra"]) == (ids[2], "first", {})
    assert s.answered("edit-extract", first=True)[7]["model_served"] == "probe-0"


def test_parse_labels_drops_unknown():
    kept, unknown, extra = parse_labels(
        '{"labels": ["bug", "made-up", "bug"], "reason": "pov"}', {"bug", "crash"}
    )
    assert extra == {"reason": "pov"}
    assert kept == ["bug"] and unknown == ["made-up"]


def test_guideline_line_and_append(tmp_path):
    issue = {"number": 7, "title": "Crash on export"}
    line = gl.guideline_line(issue, ["bug"], ["bug", "crash", "topic:export"])
    assert "add crash, topic:export" in line and "drop" not in line
    gl.add_correction("godot", issue, ["bug", "topic:gui"], ["bug"], data_dir=tmp_path)
    text = gl.load("godot", data_dir=tmp_path)
    assert text.count("\n") == 1 and "drop topic:gui" in text and "(#7)" in text
    assert gl.load("flutter", data_dir=tmp_path) == ""


def test_guidelines_from_eval_issues_never_reach_a_prompt(tmp_path):
    gl.add_correction("godot", {"number": 7, "title": "Crash on export"}, [], ["crash"], data_dir=tmp_path)
    gl.add_correction("godot", {"number": 8, "title": "Wrong color"}, [], ["bug"], data_dir=tmp_path)
    # Issue 7 was train when corrected, and is eval after a re-pull.
    text = gl.load("godot", eval_issues={7}, data_dir=tmp_path)
    assert "Crash on export" not in text and "crash" not in text
    assert "Wrong color" in text and text.count("\n") == 1
