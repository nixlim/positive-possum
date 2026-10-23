# Mubit issue triage

Tags GitHub issues for OSS repos (Godot, Flutter) through Mubit, and measures
whether Mubit's task-adapted model has started serving the task.

Mubit records every call, groups calls with the same model + system prompt +
output format into a task, trains an adapter, and then routes the task to it.
There is no API to ask when that happens. This harness detects it from the
outside: a fixed eval set is re-run through Mubit on a schedule and scored
against the maintainers' real labels, next to a baseline from the provider
called directly.

## Setup

```
cp .env.example .env     # MUBIT_API_KEY=mbt_..., OPENAI_API_KEY=sk-... (baseline only)
uv sync
uv run pytest
```

GitHub access uses `GITHUB_TOKEN` or the logged-in `gh` CLI.

## Workflow

```
uv run triage pull --repo godot --n 300        # data/godot/{labels.json,issues.jsonl}
uv run triage eval --repo godot --route direct  # baseline, once, on your own OpenAI key
uv run triage eval --repo godot                 # through Mubit; repeat over time
uv run triage replay --repo godot --limit 60    # push training traffic through Mubit
uv run triage correct --repo godot --issue 124553 --labels bug,topic:animation  # train issues only
uv run triage watch --every 3600               # hourly Mubit evals for every repo with a dataset
uv run triage report                            # timeline: accuracy, agreement, latency, served-by
```

Every call is logged to `data/calls.sqlite` with response headers, the served
model string, the Mubit request id, latency and tokens. A change in the served
model, or a drop in agreement with the direct baseline, is the routing signal.
Confirm it in the console's Requests page ("Served by").

## Design constraints from Mubit

- The system prompt per repo is frozen. Changing one word forks a new task and
  resets the kept-example count. Maintainer corrections therefore go into a
  guidelines block in the user message (`data/guidelines/<repo>.md`).
- Chat completions on the starter credit refuse `store`, so no `metadata`.
  Each issue gets its own default session (instructions + first user message).
- Eval calls through Mubit are recorded and count toward the task. Nothing
  leaks: the recorded reply is the model's answer, not the ground truth.
  The cost: the adapter is trained on the exact eval inputs paired with the
  base model's answers, so once it serves, `agreement_vs_direct` is biased
  upward and a drop in agreement understates the switch. Read it together
  with the served model and accuracy against the maintainers' labels.
  (`x-middleware-store: false` would avoid this but also hides eval calls
  from the console's Requests page, which is the confirmation step.)
- The baseline for agreement must share the task (system prompt + schema,
  `task_sha`), guidelines and model; otherwise the run gets `baseline_mismatch`.
