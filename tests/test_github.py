from triage import github
from triage.config import REPOS
from triage.github import assign_split, clean_body, filter_labels, first_pass


def test_fetch_issues_drops_pull_requests(monkeypatch):
    old = "2026-01-01T00:00:00Z"
    items = [
        {"number": 2, "created_at": old, "labels": [{"name": "bug"}], "pull_request": {"url": "x"}},
        {"number": 1, "created_at": old, "labels": [{"name": "bug"}]},
    ]
    monkeypatch.setattr(github, "_paginate", lambda client, path, params: iter(items))
    out = github.fetch_issues(None, REPOS["godot"], 10, {"bug"})
    assert [i["number"] for i in out] == [1]


def test_clean_body_strips_template_comments_and_truncates():
    body = "<!-- fill this in -->\nHello\n\n\n\n\nWorld " + "x" * 50
    out = clean_body(body, max_chars=20)
    assert "fill this in" not in out
    assert "\n\n\n" not in out
    assert out.endswith("[truncated]")
    assert clean_body(None, 10) == ""


def test_filter_labels_godot_rules():
    cfg = REPOS["godot"]
    raw = [
        {"name": "bug", "description": "x"},
        {"name": "topic:gui", "description": None},
        {"name": "platform:macos", "description": ""},
        {"name": "needs testing", "description": ""},
        {"name": "cherrypick:4.3", "description": ""},
        {"name": "archived", "description": ""},
    ]
    names = [lab["name"] for lab in filter_labels(cfg, raw)]
    assert names == ["bug", "platform:macos", "topic:gui"]


def test_filter_labels_flutter_excludes_infra():
    cfg = REPOS["flutter"]
    raw = [{"name": n} for n in ["team-infra", "team-engine", "p: camera", "a: text input", "engine", "P1"]]
    assert [lab["name"] for lab in filter_labels(cfg, raw)] == ["a: text input", "engine", "team-engine"]


def _ev(event, label, actor, t):
    return {"event": event, "label": label, "actor": actor, "created_at": t}


def test_first_pass_is_first_human_batch_only():
    targets = {"bug", "topic:rendering", "platform:macos", "crash"}
    events = [
        _ev("labeled", "needs-triage", "prow[bot]", "2026-10-01T10:00:00Z"),
        _ev("labeled", "bug", "alice", "2026-10-01T10:05:00Z"),
        _ev("labeled", "topic:rendering", "alice", "2026-10-01T10:06:00Z"),
        _ev("labeled", "needs testing", "alice", "2026-10-01T10:06:30Z"),
        _ev("unlabeled", "topic:rendering", "bob", "2026-10-01T10:30:00Z"),  # later correction
        _ev("labeled", "platform:macos", "alice", "2026-10-02T09:00:00Z"),  # outside window
    ]
    assert first_pass(events, targets) == {"bug", "topic:rendering"}


def test_first_pass_with_no_human_events_is_empty():
    assert first_pass([_ev("labeled", "bug", "x[bot]", "2026-10-01T10:00:00Z")], {"bug"}) == set()


def test_assign_split_is_deterministic_and_stable_per_issue():
    nums = list(range(100, 200))
    s1, s2 = assign_split("godot", nums), assign_split("godot", list(reversed(nums)))
    assert s1 == s2
    assert 5 <= sum(v == "eval" for v in s1.values()) <= 40
    # A bigger pull, or one shifted by dropping and adding issues, keeps every shared issue's split.
    bigger = assign_split("godot", list(range(50, 400)))
    shifted = assign_split("godot", list(range(103, 260)))
    assert all(bigger[n] == s1[n] for n in nums)
    assert all(shifted[n] == s1[n] for n in range(103, 200))


def test_assign_split_keeps_previous_assignment():
    prev = {n: "train" for n in range(100, 200)} | {150: "eval"}
    s = assign_split("godot", list(range(90, 210)), existing=prev)
    assert all(s[n] == prev[n] for n in prev)
    fresh = assign_split("godot", list(range(90, 100)) + list(range(200, 210)))
    assert all(s[n] == fresh[n] for n in fresh)
