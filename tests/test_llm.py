import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest
from anthropic.types import Message

from triage.llm import classify, client
from triage.store import Store

LABELS = [{"name": "bug", "description": ""}, {"name": "crash", "description": ""}]
ISSUE = {"number": 9, "title": "Crash on start", "body": "It crashes."}
URL = "https://api.mubit.ai/anthropic/v1/messages"


def _client(create):
    return SimpleNamespace(messages=SimpleNamespace(with_raw_response=SimpleNamespace(create=create)))


def _message(content, stop_reason="end_turn"):
    return Message.model_validate(
        {
            "id": "m1",
            "type": "message",
            "role": "assistant",
            "model": "adapter-v1",
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 3},
        }
    )


def _text(s):
    return {"type": "text", "text": s}


def _raw(parse):
    return SimpleNamespace(headers=httpx.Headers({"x-middleware-request-id": "mreq_ok"}), parse=parse)


def _logged(tmp_path, res):
    s = Store(tmp_path / "t.sqlite")
    s.log_call("r", "eval", "godot", ISSUE["number"], res)
    row = s.db.execute("SELECT request_id, model_served, error, headers, raw, labels FROM calls").fetchone()
    return s, row


def test_request_uses_structured_output_and_the_issue_session(monkeypatch):
    seen = {}

    def create(**req):
        seen.update(req)
        return _raw(lambda: _message([_text('{"labels": ["bug"]}')], stop_reason="end_turn"))

    classify(_client(create), "godotengine/godot", LABELS, ISSUE, "", model="claude-sonnet-5-5")
    assert seen["model"] == "claude-sonnet-5-5"
    assert seen["output_config"]["format"]["type"] == "json_schema"
    assert seen["output_config"]["format"]["schema"]["properties"]["labels"]["items"]["enum"] == [
        "bug",
        "crash",
    ]
    assert seen["metadata"] == {"user_id": "godotengine/godot#9"}
    assert [m["role"] for m in seen["messages"]] == ["user"]


def test_gateway_error_keeps_headers_and_mubit_request_id(tmp_path):
    body = {"type": "error", "error": {"type": "middleware_gateway_error", "code": "starter_credit_option"}}
    resp = httpx.Response(
        403,
        headers={"x-middleware-request-id": "mreq_err", "x-should-retry": "false"},
        json=body,
        request=httpx.Request("POST", URL),
    )

    def create(**req):
        raise anthropic.APIStatusError("refused", response=resp, body=body)

    res = classify(_client(create), "godotengine/godot", LABELS, ISSUE, "")
    s, (rid, served, err, headers, raw, labels) = _logged(tmp_path, res)
    assert rid == "mreq_err"
    assert json.loads(headers)["x-should-retry"] == "false"
    assert json.loads(raw) == {"status_code": 403, "body": body}
    assert "403" in err and served is None and json.loads(labels) == []
    assert s.predictions("r") == {}


def test_connection_error_is_logged_not_raised(tmp_path):
    def create(**req):
        raise anthropic.APIConnectionError(request=httpx.Request("POST", URL))

    res = classify(_client(create), "godotengine/godot", LABELS, ISSUE, "")
    s, (rid, _, err, _, _, _) = _logged(tmp_path, res)
    assert rid is None and err.startswith("APIConnectionError")
    assert s.predictions("r") == {}


def _bad_parse():
    raise ValueError("not json")


def test_malformed_replies_are_logged_errors(tmp_path):
    cases = {
        "refusal": lambda: _message([], stop_reason="refusal"),
        "empty content": lambda: _message([], stop_reason="end_turn"),
        "not json": lambda: _message([_text("bug")], stop_reason="end_turn"),
        "labels not a list": lambda: _message([_text('{"labels": "bug"}')], stop_reason="end_turn"),
        "labels not strings": lambda: _message([_text('{"labels": [{"x": 1}]}')], stop_reason="end_turn"),
        "unparseable body": _bad_parse,
    }
    for i, (name, parse) in enumerate(cases.items()):
        res = classify(
            _client(lambda parse=parse, **req: _raw(parse)), "godotengine/godot", LABELS, ISSUE, ""
        )
        assert res.error and res.labels == [], name
        s, (rid, served, err, headers, _, _) = _logged(tmp_path / str(i), res)
        assert rid == "mreq_ok" and json.loads(headers)["x-middleware-request-id"] == "mreq_ok", name
        assert err.startswith("unparseable reply"), name
        assert s.predictions("r") == {}, name
        if name != "unparseable body":
            assert served == "adapter-v1", name


def test_client_sends_only_the_mubit_key_and_only_to_mubit(monkeypatch):
    monkeypatch.setenv("MUBIT_API_KEY", "mbt_test")
    # A developer's own Anthropic setup must never leak into the request: Mubit refuses a
    # second credential (401 extra_credential_refused), and a direct call breaks the rules.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-decoy")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "bearer-decoy")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.delenv("ANTHROPIC_CUSTOM_HEADERS", raising=False)
    c = client()
    assert str(c.base_url).rstrip("/") == "https://api.mubit.ai/anthropic"
    assert c.api_key == "mbt_test" and c.auth_token is None
    sent = {k.lower(): v for k, v in c.auth_headers.items()}
    assert "decoy" not in json.dumps(sent)
    assert sent in ({"x-api-key": "mbt_test"}, {"authorization": "Bearer mbt_test"})
    every = {k.lower(): v for k, v in c.default_headers.items()}
    assert "decoy" not in json.dumps(every)
    assert not ({"authorization", "x-api-key"} - set(sent)) & set(every)  # no auth header but the Mubit key's


def test_client_refuses_custom_headers_from_the_environment(monkeypatch):
    # The SDK would merge these into every request, e.g. a second credential.
    monkeypatch.setenv("MUBIT_API_KEY", "mbt_test")
    monkeypatch.setenv("ANTHROPIC_CUSTOM_HEADERS", "Authorization: Bearer decoy")
    with pytest.raises(SystemExit, match="ANTHROPIC_CUSTOM_HEADERS"):
        client()


def test_good_reply(tmp_path):
    raw = _raw(lambda: _message([_text('{"labels": ["crash", "made-up"]}')], stop_reason="end_turn"))
    res = classify(_client(lambda **req: raw), "godotengine/godot", LABELS, ISSUE, "")
    assert res.error is None and res.labels == ["crash"] and res.unknown_labels == ["made-up"]
    assert res.model_served == "adapter-v1" and res.request_id == "mreq_ok" and res.prompt_tokens == 10
