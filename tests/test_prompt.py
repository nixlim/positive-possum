import hashlib
import json
from pathlib import Path

from triage.config import repo
from triage.github import load_dataset
from triage.prompt import build_request, response_format, system_prompt, user_message

LABELS = [{"name": "bug", "description": "Something broken"}, {"name": "topic:gui", "description": ""}]
ISSUE = {"number": 42, "title": "Button ignores clicks", "body": "Steps: click it."}

# Golden hashes of the frozen Mubit task identity. If one of these fails, the change forks
# the Mubit task and resets its kept-example count: only update them on purpose.
SYSTEM_SHA = "7347c30dea151681b6e57b96ebe3c839b8bb1b42594f901beba41e360abcf356"
FORMAT_SHA = "57dd0be6b577f3e4fe06e97b42a4e53efefcf1432564bb158fe8e756d6a2b48f"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_system_prompt_is_frozen_per_repo():
    a = system_prompt("godotengine/godot", LABELS)
    assert _sha(a) == SYSTEM_SHA
    assert _sha(json.dumps(response_format(LABELS), sort_keys=True)) == FORMAT_SHA
    assert "- bug: Something broken" in a
    assert "- topic:gui\n" in a


# The same, for the real Godot task built from the committed data/godot/labels.json. A re-pull
# that changes a label name or description changes these and forks the task.
GODOT_SYSTEM_SHA = "c093b18de88e855135f4aff023a1a89f30c611cc550e6658f17d3e028fd85ec7"
GODOT_FORMAT_SHA = "64d6c8196cec1e1644887f4282c43d88256fcafa79b4b95beb90f1e2580b0fee"


def test_real_godot_task_identity_is_frozen():
    labels, _ = load_dataset(repo("godot"), Path(__file__).parent.parent / "data")
    assert _sha(system_prompt("godotengine/godot", labels)) == GODOT_SYSTEM_SHA
    assert _sha(json.dumps(response_format(labels), sort_keys=True)) == GODOT_FORMAT_SHA


def test_task_identity_identical_across_issues_and_guidelines():
    other = {"number": 7, "title": "Crash on export", "body": "It crashes."}
    r1 = build_request("gpt-6-luna", "godotengine/godot", LABELS, ISSUE, "")
    r2 = build_request("gpt-6-luna", "godotengine/godot", LABELS, other, "- add crash for export crashes")
    assert r1["model"] == r2["model"]
    assert r1["messages"][0] == r2["messages"][0]
    assert json.dumps(r1["response_format"], sort_keys=True) == json.dumps(r2["response_format"], sort_keys=True)
    assert set(r1) == set(r2) == {"model", "messages", "response_format"}
    assert r1["messages"][1] != r2["messages"][1]


def test_guidelines_live_only_in_user_message():
    req = build_request("gpt-6-luna", "godotengine/godot", LABELS, ISSUE, "- add topic:gui for button bugs")
    system, user = req["messages"]
    assert "topic:gui for button bugs" not in system["content"]
    assert "topic:gui for button bugs" in user["content"]
    assert "Issue #42: Button ignores clicks" in user["content"]


def test_empty_guidelines_placeholder():
    assert "(none yet)" in user_message(ISSUE, "")


def test_schema_enum_matches_labels():
    rf = response_format(LABELS)
    assert rf["json_schema"]["strict"] is True
    assert rf["json_schema"]["schema"]["properties"]["labels"]["items"]["enum"] == ["bug", "topic:gui"]
