"""The frozen task prompts. Mubit groups calls into a task by model + system
prompt + output format, so nothing in here may vary between calls of the
same repo except the user message. Guidelines go in the user message.

Two task kinds share one request shape: "github" (label an issue) and "wiki"
(decide whether an edit gets reverted). Their system templates and output
schemas are separate; never edit an existing one, it forks that task at Mubit.

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

WIKI_SYSTEM_TEMPLATE = """\
You are a recent-changes patroller for {full_name}.
Read the edit and decide whether the patrollers of this wiki would revert it.

Rules:
- Use exactly one label from the allowed list below.
- Judge the edit the way this community does, not by your own taste: an edit is reverted when patrollers would undo it, whatever the reason.
- Follow any patroller guidelines included with the edit; they override your own judgement.
- Also give the main reason for your verdict and how sure you are, using the values in the output schema.
- Return only the JSON object required by the output schema.

Allowed labels:
{label_lines}
"""

TEMPLATES = {"github": SYSTEM_TEMPLATE, "wiki": WIKI_SYSTEM_TEMPLATE}
HEADINGS = {
    "github": ("Maintainer guidelines", "Issue #{number}: {title}"),
    "wiki": ("Patroller guidelines", 'Edit {number} to "{title}"'),
}

# Extra fields of the wiki reply. They are never scored (the only ground truth is
# keep/revert) but they drive the patrol desk: the chip and the confidence bar.
WIKI_REASONS = [
    "constructive",
    "copyedit",
    "vandalism",
    "promotional",
    "unsourced",
    "test_edit",
    "pov",
    "other",
]
CONFIDENCES = ["low", "medium", "high"]

MAX_TOKENS = 512  # a label list is a few dozen tokens; fixed so the request shape never varies


def system_prompt(full_name: str, labels: list[dict], kind: str = "github") -> str:
    lines = "\n".join(
        f"- {lab['name']}: {lab['description']}" if lab.get("description") else f"- {lab['name']}"
        for lab in labels
    )
    return TEMPLATES[kind].format(full_name=full_name, label_lines=lines)


def output_schema(labels: list[dict], kind: str = "github") -> dict:
    """JSON schema of the reply: a list of labels from the allowed enum, plus for the
    wiki task the reason and confidence behind the verdict."""
    props: dict = {
        "labels": {
            "type": "array",
            "items": {"type": "string", "enum": [lab["name"] for lab in labels]},
        }
    }
    required = ["labels"]
    if kind == "wiki":
        props["reason"] = {"type": "string", "enum": WIKI_REASONS}
        props["confidence"] = {"type": "string", "enum": CONFIDENCES}
        required += ["reason", "confidence"]
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


def user_message(issue: dict, guidelines: str, kind: str = "github") -> str:
    g = guidelines.strip() or "(none yet)"
    heading, item = HEADINGS[kind]
    return f"{heading}:\n{g}\n\n{item.format(**issue)}\n\n{issue['body']}"


def session_id(full_name: str, issue: dict) -> str:
    """Mubit's session key for Anthropic calls (`metadata.user_id`): one per issue, so a
    re-send after a correction lands in the same session as the original answer."""
    return f"{full_name}#{issue['number']}"


def build_request(
    model: str, full_name: str, labels: list[dict], issue: dict, guidelines: str, kind: str = "github"
) -> dict:
    """Keyword arguments for `anthropic.Anthropic().messages.create`."""
    return {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "system": system_prompt(full_name, labels, kind),
        "messages": [{"role": "user", "content": user_message(issue, guidelines, kind)}],
        "output_config": {"format": {"type": "json_schema", "schema": output_schema(labels, kind)}},
        "metadata": {"user_id": session_id(full_name, issue)},
    }
