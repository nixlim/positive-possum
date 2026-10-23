import json
from types import SimpleNamespace

import httpx
import openai
from openai.types.chat import ChatCompletion

from triage.llm import classify, client_for
from triage.store import Store

LABELS = [{"name": "bug", "description": ""}, {"name": "crash", "description": ""}]
ISSUE = {"number": 9, "title": "Crash on start", "body": "It crashes."}
URL = "https://api.mubit.ai/openai/v1/chat/completions"


def _client(create):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        with_raw_response=SimpleNamespace(create=create))))


def _completion(choices):
    return ChatCompletion.model_validate({
        "id": "c1", "object": "chat.completion", "created": 0, "model": "adapter-v1", "choices": choices,
        "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13},
    })


def _choice(content, refusal=None):
    return {"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": content, "refusal": refusal}}


def _raw(parse):
    return SimpleNamespace(headers=httpx.Headers({"x-middleware-request-id": "mreq_ok"}), parse=parse)


def _logged(tmp_path, res):
    s = Store(tmp_path / "t.sqlite")
    s.log_call("r", "eval", "godot", ISSUE["number"], res)
    row = s.db.execute("SELECT request_id, model_served, error, headers, raw, labels FROM calls").fetchone()
    return s, row


def test_gateway_error_keeps_headers_and_mubit_request_id(tmp_path):
    body = {"error": {"type": "middleware_gateway_error", "code": "starter_credit_option"}}
    resp = httpx.Response(403, headers={"x-middleware-request-id": "mreq_err", "x-should-retry": "false"},
                          json=body, request=httpx.Request("POST", URL))

    def create(**req):
        raise openai.APIStatusError("refused", response=resp, body=body)

    res = classify(_client(create), "mubit", "godotengine/godot", LABELS, ISSUE, "")
    s, (rid, served, err, headers, raw, labels) = _logged(tmp_path, res)
    assert rid == "mreq_err"
    assert json.loads(headers)["x-should-retry"] == "false"
    assert json.loads(raw) == {"status_code": 403, "body": body}
    assert "403" in err and served is None and json.loads(labels) == []
    assert s.predictions("r") == {}


def test_connection_error_is_logged_not_raised(tmp_path):
    def create(**req):
        raise openai.APIConnectionError(request=httpx.Request("POST", URL))

    res = classify(_client(create), "mubit", "godotengine/godot", LABELS, ISSUE, "")
    s, (rid, _, err, _, _, _) = _logged(tmp_path, res)
    assert rid is None and err.startswith("APIConnectionError")
    assert s.predictions("r") == {}


def _bad_parse():
    raise ValueError("not json")


def test_malformed_replies_are_logged_errors(tmp_path):
    cases = {
        "refusal": lambda: _completion([_choice(None, refusal="I can't help with that")]),
        "no choices": lambda: _completion([]),
        "labels not a list": lambda: _completion([_choice('{"labels": "bug"}')]),
        "labels not strings": lambda: _completion([_choice('{"labels": [{"x": 1}]}')]),
        "unparseable body": _bad_parse,
    }
    for i, (name, parse) in enumerate(cases.items()):
        res = classify(_client(lambda **req: _raw(parse)), "mubit", "godotengine/godot", LABELS, ISSUE, "")
        assert res.error and res.labels == [], name
        s, (rid, served, err, headers, _, _) = _logged(tmp_path / str(i), res)
        assert rid == "mreq_ok" and json.loads(headers)["x-middleware-request-id"] == "mreq_ok", name
        assert err.startswith("unparseable reply"), name
        assert s.predictions("r") == {}, name
        if name != "unparseable body":
            assert served == "adapter-v1", name


def test_each_route_sends_only_its_own_key_to_its_own_host(monkeypatch):
    monkeypatch.setenv("MUBIT_API_KEY", "mbt_test")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://api.mubit.ai/openai/v1")  # as Mubit's docs suggest
    mubit, direct = client_for("mubit"), client_for("direct")
    assert str(mubit.base_url).rstrip("/") == "https://api.mubit.ai/openai/v1" and mubit.api_key == "mbt_test"
    assert str(direct.base_url).rstrip("/") == "https://api.openai.com/v1" and direct.api_key == "sk-test"


def test_good_reply(tmp_path):
    raw = _raw(lambda: _completion([_choice('{"labels": ["crash", "made-up"]}')]))
    res = classify(_client(lambda **req: raw), "mubit", "godotengine/godot", LABELS, ISSUE, "")
    assert res.error is None and res.labels == ["crash"] and res.unknown_labels == ["made-up"]
    assert res.model_served == "adapter-v1" and res.request_id == "mreq_ok" and res.prompt_tokens == 10
