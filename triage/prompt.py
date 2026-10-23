"""The frozen task prompt. Mubit groups calls into a task by model + system
prompt + output format, so nothing in here may vary between calls of the
same repo except the user message. Guidelines go in the user message.

Sonnet 5.5 refuses forced tool choice (`tool_choice` type "tool"/"any"), so the
output format is Anthropic structured output (`output_config.format`), which
guarantees a JSON reply matching the schema."""
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

MAX_TOKENS = 512  # a label list is a few dozen tokens; fixed so the request shape never varies


def system_prompt(full_name: str, labels: list[dict]) -> str:
    lines = "\n".join(
        f"- {l['name']}: {l['description']}" if l.get("description") else f"- {l['name']}"
        for l in labels
    )
    return SYSTEM_TEMPLATE.format(full_name=full_name, label_lines=lines)


def output_schema(labels: list[dict]) -> dict:
    """JSON schema of the reply: a list of labels from the allowed enum."""
    return {
        "type": "object",
        "properties": {
            "labels": {
                "type": "array",
                "items": {"type": "string", "enum": [l["name"] for l in labels]},
            }
        },
        "required": ["labels"],
        "additionalProperties": False,
    }


def user_message(issue: dict, guidelines: str) -> str:
    g = guidelines.strip() or "(none yet)"
    return (
        f"Maintainer guidelines:\n{g}\n\n"
        f"Issue #{issue['number']}: {issue['title']}\n\n{issue['body']}"
    )


def session_id(full_name: str, issue: dict) -> str:
    """Mubit's session key for Anthropic calls (`metadata.user_id`): one per issue, so a
    re-send after a correction lands in the same session as the original answer."""
    return f"{full_name}#{issue['number']}"


def build_request(model: str, full_name: str, labels: list[dict], issue: dict, guidelines: str) -> dict:
    """Keyword arguments for `anthropic.Anthropic().messages.create`."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt(full_name, labels),
        "messages": [{"role": "user", "content": user_message(issue, guidelines)}],
        "output_config": {"format": {"type": "json_schema", "schema": output_schema(labels)}},
        "metadata": {"user_id": session_id(full_name, issue)},
    }
