"""Hand check of the Mubit facts the triage request shape relies on (MUBIT_API_NOTES.md,
"Facts the triage request relies on"). Needs MUBIT_API_KEY in .env.

Run:  uv run python mubit_smoke.py

1. The exact triage request (triage.prompt.build_request, sent by triage.llm.classify
   through triage.llm.client): output_config json_schema + metadata.user_id.
2. Probe: the same request with a forced tool (tool_choice type "tool") instead.
3. Probe: the same request with a dummy second credential header (not a real key).

Prints status, Mubit error code and x-middleware-request-id for each, never a key.
The repo name is made up, so these calls form their own Mubit task and never count
toward a real repo's task. Exits 1 if check 1 fails.
"""
from __future__ import annotations

import sys

from anthropic import APIStatusError

from triage.config import MODEL
from triage.llm import classify, client
from triage.prompt import build_request, output_schema

REPO = "mubit-triage/smoke"
LABELS = [{"name": "bug", "description": "Something is broken"},
          {"name": "documentation", "description": "Docs only"}]
ISSUE = {"number": 1, "title": "Editor crashes on start", "body": "Opening the editor crashes it."}


def _error_code(e: APIStatusError) -> str | None:
    err = e.body.get("error") if isinstance(e.body, dict) else None
    return err.get("code") or err.get("type") if isinstance(err, dict) else None


def _probe(label: str, c, req: dict, **extra) -> None:
    try:
        raw = c.messages.with_raw_response.create(**req, **extra)
        print(f"{label}: 200 accepted, served_by={raw.parse().model} "
              f"mreq={raw.headers.get('x-middleware-request-id')}")
    except APIStatusError as e:
        print(f"{label}: {e.status_code} code={_error_code(e)} "
              f"mreq={e.response.headers.get('x-middleware-request-id')}")


c = client()

res = classify(c, REPO, LABELS, ISSUE, "", model=MODEL)
ok = res.error is None
print(f"1 output_config + metadata.user_id on {MODEL}: {'PASS' if ok else 'FAIL'} "
      f"labels={res.labels} served_by={res.model_served} mreq={res.request_id}"
      + (f" error={res.error}" if res.error else ""))

forced = build_request(MODEL, REPO, LABELS, ISSUE, "")
del forced["output_config"]
forced["tools"] = [{"name": "issue_labels", "description": "Report the issue's labels.",
                    "input_schema": output_schema(LABELS)}]
forced["tool_choice"] = {"type": "tool", "name": "issue_labels"}
_probe(f"2 forced tool_choice on {MODEL} (notes: refused)", c, forced)

# Deliberately a dummy second credential, to read the code Mubit answers with.
_probe("3 second credential (notes: 401 extra_credential_refused)", c,
       build_request(MODEL, REPO, LABELS, ISSUE, ""),
       extra_headers={"Authorization": "Bearer not-a-credential"})

sys.exit(0 if ok else 1)
