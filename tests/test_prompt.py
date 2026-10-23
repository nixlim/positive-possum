import hashlib
import json
from pathlib import Path

from triage.config import repo
from triage.github import load_dataset
from triage.prompt import MAX_TOKENS, build_request, output_schema, system_prompt, user_message

LABELS = [{"name": "bug", "description": "Something broken"}, {"name": "topic:gui", "description": ""}]
ISSUE = {"number": 42, "title": "Button ignores clicks", "body": "Steps: click it."}

# Golden hashes of the frozen Mubit task identity. If one of these fails, the change forks
# the Mubit task and resets its kept-example count: only update them on purpose.
SYSTEM_SHA = "7347c30dea151681b6e57b96ebe3c839b8bb1b42594f901beba41e360abcf356"
SCHEMA_SHA = "b6bb88fd8f7f401a732c798f5808eddc07413d3e064d635cc7627449102f97ec"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def test_system_prompt_is_frozen_per_repo():
    a = system_prompt("godotengine/godot", LABELS)
    assert _sha(a) == SYSTEM_SHA
    assert _sha(json.dumps(output_schema(LABELS), sort_keys=True)) == SCHEMA_SHA
    assert "- bug: Something broken" in a
    assert "- topic:gui\n" in a


# The same, for the real Godot task built from the committed data/godot/labels.json. A re-pull
# that changes a label name or description changes these and forks the task.
GODOT_SYSTEM_SHA = "c093b18de88e855135f4aff023a1a89f30c611cc550e6658f17d3e028fd85ec7"
GODOT_SCHEMA_SHA = "657ee59cce1991b74cefebdf93bed18460ba550a9be0ba65b1aa566a7d7f4e33"


def test_real_godot_task_identity_is_frozen():
    labels, _ = load_dataset(repo("godot"), Path(__file__).parent.parent / "data")
    assert _sha(system_prompt("godotengine/godot", labels)) == GODOT_SYSTEM_SHA
    assert _sha(json.dumps(output_schema(labels), sort_keys=True)) == GODOT_SCHEMA_SHA


WIKI_LABELS = [{"name": "keep", "description": "stays"}, {"name": "revert", "description": "undone"}]
WIKI_SYSTEM_SHA = "a1ee2c7968e26bc4e13cd130ca4d5e64ee2f1143951becd923d6db36f9d5ada0"
EDIT = {"number": 1381634663, "title": "Compsopogonophyceae", "body": "Editor: anonymous (IP)\nDiff:\n+lol"}


def test_wiki_prompt_is_frozen_and_separate_from_github():
    assert _sha(system_prompt("en.wikipedia.org", WIKI_LABELS, "wiki")) == WIKI_SYSTEM_SHA
    req = build_request("claude-sonnet-5-5", "en.wikipedia.org", WIKI_LABELS, EDIT, "- rule", kind="wiki")
    assert "recent-changes patroller for en.wikipedia.org" in req["system"]
    assert "GitHub" not in req["system"]
    user = req["messages"][0]["content"]
    assert user.startswith('Patroller guidelines:\n- rule\n\nEdit 1381634663 to "Compsopogonophyceae"\n\nEditor:')
    assert req["metadata"] == {"user_id": "en.wikipedia.org#1381634663"}
    assert req["output_config"]["format"]["schema"]["properties"]["labels"]["items"]["enum"] == ["keep", "revert"]


def test_task_identity_identical_across_issues_and_guidelines():
    other = {"number": 7, "title": "Crash on export", "body": "It crashes."}
    r1 = build_request("claude-sonnet-5-5", "godotengine/godot", LABELS, ISSUE, "")
    r2 = build_request("claude-sonnet-5-5", "godotengine/godot", LABELS, other, "- add crash for export crashes")
    for key in ("model", "max_tokens", "system", "output_config"):
        assert r1[key] == r2[key], key
    assert set(r1) == set(r2) == {"model", "max_tokens", "system", "messages", "output_config", "metadata"}
    assert r1["max_tokens"] == MAX_TOKENS
    assert r1["messages"] != r2["messages"]
    # Session per issue, so a correction re-send joins the original answer's session.
    assert r1["metadata"] == {"user_id": "godotengine/godot#42"}
    assert r2["metadata"] == {"user_id": "godotengine/godot#7"}


def test_guidelines_live_only_in_user_message():
    req = build_request("claude-sonnet-5-5", "godotengine/godot", LABELS, ISSUE, "- add topic:gui for button bugs")
    assert "topic:gui for button bugs" not in req["system"]
    (user,) = req["messages"]
    assert user["role"] == "user"
    assert "topic:gui for button bugs" in user["content"]
    assert "Issue #42: Button ignores clicks" in user["content"]


def test_empty_guidelines_placeholder():
    assert "(none yet)" in user_message(ISSUE, "")


def test_output_schema_enum_matches_labels():
    schema = output_schema(LABELS)
    assert schema["properties"]["labels"]["items"]["enum"] == ["bug", "topic:gui"]
    assert schema["additionalProperties"] is False
    req = build_request("claude-sonnet-5-5", "godotengine/godot", LABELS, ISSUE, "")
    assert req["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert "tools" not in req and "tool_choice" not in req  # Sonnet 5.5 refuses forced tool choice
