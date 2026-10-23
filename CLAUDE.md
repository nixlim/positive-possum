# Mubit issue triage

Hackathon project (Mubit Night Shift, 23-25 Oct 2026): tag GitHub issues for OSS repos through
Mubit and measure, from outside Mubit, when its task-adapted model starts serving the task.

## Commands

- `uv sync` installs; `uv run pytest -q` is the gate.
- `uv run triage pull|eval|replay|correct|watch|report` (see README.md).
- The only secret is `MUBIT_API_KEY` in `.env`. Every model call goes through Mubit (hackathon
  rule); there is no direct provider route, and no other key may be sent.

## Conventions

- `triage/prompt.py` is the frozen Mubit task prompt per repo. Do not change wording, schema or
  model casually: Mubit groups calls into tasks by model + system prompt + output schema, and any
  change forks a new task with a fresh kept-example count. Per-call variation goes in the user
  message only.
- `data/<repo>/` holds the committed dataset (labels and issues). `data/calls.sqlite` is the
  uncommitted evidence log. `data/guidelines/<repo>.md` accumulates maintainer corrections.
- Facts about the Mubit API that were verified by hand are in `MUBIT_API_NOTES.md`.
- Commits go through `lean-forge commit`; landing through `lean-forge land`.
