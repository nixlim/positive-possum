# Mubit issue triage

Tags GitHub issues for OSS repos (Godot, Flutter) through Mubit, and measures
whether Mubit's task-adapted model has started serving the task.

Mubit records every call, groups calls with the same model + system prompt +
output schema into a task, trains an adapter, and then routes the task to it.
There is no API to ask when that happens. This harness detects it from the
outside: a fixed eval set is re-run through Mubit on a schedule and scored
against the maintainers' real labels, and compared with a reference run: the
first run that every reply shows was served by the provider (Claude Sonnet
5.5) itself, checked against the served-model string of each call.

Every model call goes through Mubit (hackathon rule). There is no direct
provider route in this code base, and the Mubit key is the only credential
the harness ever sends.

## Setup

```
cp .env.example .env     # MUBIT_API_KEY=mbt_...
uv sync
uv run pytest
```

GitHub access uses `GITHUB_TOKEN` or the logged-in `gh` CLI.

## Workflow

The shell profile on this machine exports `ANTHROPIC_BASE_URL` and
`ANTHROPIC_CUSTOM_HEADERS` for other tools. The client ignores the base URL,
but refuses to start while the custom headers variable is set, so clear both
first:

```
unset ANTHROPIC_CUSTOM_HEADERS ANTHROPIC_BASE_URL
set -a; . ./.env; set +a
```

```
uv run triage pull --repo godot --n 300        # data/godot/{labels.json,issues.jsonl}
uv run triage eval --repo godot                 # through Mubit; the first provider-served run is the reference
uv run triage replay --repo godot --limit 60    # push training traffic through Mubit
uv run triage correct --repo godot --issue 124553 --labels bug,topic:animation  # train issues only
uv run triage watch --every 3600               # hourly Mubit evals for every repo with a dataset
uv run triage report                            # timeline: accuracy, agreement, latency, served-by
```

Every call is logged to `data/calls.sqlite` with response headers, the served
model string, the Mubit request id, latency and tokens. A change in the served
model, or a drop in agreement with the reference run below the floor between
two provider runs, is the routing signal. Confirm it in the console's Requests
page ("Served by").

## Design constraints from Mubit

- The system prompt and output schema per repo are frozen. Changing one word
  forks a new task and resets the kept-example count. Maintainer corrections
  therefore go into a guidelines block in the user message
  (`data/guidelines/<repo>.md`).
- Each issue is its own Mubit session (`metadata.user_id`), so a re-send after
  a correction lands beside the original answer, and Mubit's own held-out
  test cases (chosen by session) stay disjoint per issue.
- Eval calls through Mubit are recorded and count toward the task. Nothing
  leaks: the recorded reply is the model's answer, not the ground truth.
  The cost: the adapter is trained on the exact eval inputs paired with the
  provider's answers, so once it serves, `agreement_vs_reference` is biased
  upward and a drop in agreement understates the switch. Read it together
  with the served model and accuracy against the maintainers' labels.
  (`x-middleware-store: false` would avoid this but also hides eval calls
  from the console's Requests page, which is the confirmation step.)
- The reference run must share the task (system prompt + output schema,
  `task_sha`), guidelines and model, and every answered call in it must have
  been served by the requested model (date suffix allowed), not an adapter.
  A correction changes the guidelines without forking the Mubit task, so the
  first run after it may already be served by the adapter; it is then no
  reference. Without one the run gets `reference_mismatch` with the reason;
  with one, `reference_served` records who served the reference. Run the eval
  twice on the first night to learn the agreement floor between two runs of
  the provider itself.
- `uv run python mubit_smoke.py` re-checks by hand the API facts the request
  shape relies on (see `MUBIT_API_NOTES.md`).
