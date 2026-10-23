"""The frozen task prompt. Mubit groups calls into a task by model + system
prompt + tools + output format, so nothing in here may vary between calls of
the same repo except the user message. Guidelines go in the user message."""
from __future__ import annotations

SYSTEM_TEMPLATE = """\
You are the issue triage assistant for the {full_name} GitHub repository.
Read the issue and choose the labels a maintainer would apply.

Rules:
- Use only labels from the allowed list below. Never invent labels.
- Apply every label that fits: the type of issue plus every area it touches.
- Follow any maintainer guidelines included with the issue; they override your own judgement.
- Return only the JSON object required by the output schema.

Allowed labels:
{label_lines}
"""


def system_prompt(full_name: str, labels: list[dict]) -> str:
    lines = "\n".join(
        f"- {l['name']}: {l['description']}" if l.get("description") else f"- {l['name']}"
        for l in labels
    )
    return SYSTEM_TEMPLATE.format(full_name=full_name, label_lines=lines)


def response_format(labels: list[dict]) -> dict:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "issue_labels",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "labels": {
                        "type": "array",
                        "items": {"type": "string", "enum": [l["name"] for l in labels]},
                    }
                },
                "required": ["labels"],
                "additionalProperties": False,
            },
        },
    }


def user_message(issue: dict, guidelines: str) -> str:
    g = guidelines.strip() or "(none yet)"
    return (
        f"Maintainer guidelines:\n{g}\n\n"
        f"Issue #{issue['number']}: {issue['title']}\n\n{issue['body']}"
    )


def build_request(model: str, full_name: str, labels: list[dict], issue: dict, guidelines: str) -> dict:
    return {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt(full_name, labels)},
            {"role": "user", "content": user_message(issue, guidelines)},
        ],
        "response_format": response_format(labels),
    }
