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

### Wikipedia edit patrol (`--repo enwiki`)

The second task kind is recent-changes patrol: given a diff, the edit summary and
the editor type, decide `keep` or `revert`. Ground truth is the community's own
verdict: the MediaWiki `mw-reverted` tag on edits 12 hours to 29 days old. No key
is needed, only a User-Agent. `triage pull --repo enwiki --n 200` fetches a
balanced set (half reverted, half kept) with `revid` as the item number, and the
same `eval`, `replay`, `correct`, `watch` and `report` commands apply. The task
has its own frozen system prompt (`WIKI_SYSTEM_TEMPLATE`), so it is a separate
Mubit task from the GitHub ones.

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

## Patrol desk (live demo)

```
uv run triage serve --repo enwiki --rate 2     # http://127.0.0.1:8321
```

One page, three zones. Left: edits from the Wikimedia recent-changes stream, each
classified through Mubit as it arrives (verdict, reason, confidence, the served
model string, latency, tokens). Centre: the share of answers served by something
other than the task model, p50 per served model, and the frontier cost per day
projected from logged tokens at Sonnet 5.5 list price. Right: Keep / Revert.
A click that disagrees with the model appends one guideline line and re-sends
the same edit in the same Mubit session, so the recorded reply is the corrected
answer; a click that agrees records nothing new. Each edit takes one verdict; a
second click on the same edit gets 409. Mubit exposes no training status, so the
console line is typed by hand and labelled as such.

The desk has no authentication. A verdict spends Mubit credit and writes a line to
`data/guidelines/enwiki.md`, which reaches every later enwiki prompt. So `serve`
refuses a non-loopback `--host` unless you also pass `--allow-remote`. Pass it only
on a network you trust.

The reason and confidence on each card come from two required fields that were added
to the wiki output schema on 2026-10-23, together with one new rule line in the wiki
system template. That deliberately forked the enwiki Mubit task. Enwiki eval runs from
before then have a different `task_sha` and are never used as a reference, and the
kept-example count started again from zero. See "Deliberate task forks" in
`MUBIT_API_NOTES.md`. `uv run python mubit_smoke.py` sends the wiki request shape too.

Every live and correction call is in `data/calls.sqlite` (kinds `live`, `correct`).

## Local edit classifier (no LLM)

A free, local classifier sits alongside the Mubit desk. It is backed by a corpus of real
enwiki edits and asks whether edits can be classified by intention. Nothing in it calls
Mubit, Anthropic or any other language model.

```
uv run triage corpus --windows 24 --diffs 10000     # about 15k Wikipedia API requests, cached
uv run jupyter nbconvert --to notebook --execute --inplace notebooks/edit_intent.ipynb
```

`triage corpus` samples 24 one-hour windows over days 4 to 27 before today, rotating the hour
of day. It lists every article edit in them, bots included. It links each `mw-reverted` edit to
the edit that reverted it, using one listing per reverting tag, and records the revert summary,
tool and delay. It fetches REST compare diffs for the reverted human content edits and for a
uniform sample of kept ones, then the editors' edit counts. Finally it scores up to 400
test-window edits with Wikimedia's keyless `revertrisk-language-agnostic` model on Lift Wing.

The live desk's edits (from `data/calls.sqlite`) are listed and diffed too, so the local model
can be set against the logged Sonnet verdicts. Every response is cached under `data/corpus/`
(gitignored), so a re-run makes no requests. The anchor date and snapshot time are kept in
`data/corpus/manifest.json`.

The run follows Wikimedia's 2026 rate limits: at most 160 requests a minute across all hosts,
3 at a time, with `maxlag=5`. It honours `Retry-After` on 429, 503 and maxlag, and stops at
`--budget`.

The modules split by concern:

| Module | Job |
| --- | --- |
| `triage/corpus.py` | Pure sampling and linking helpers, plus the network session |
| `triage/corpus_build.py` | Runs the build and loads the corpus |
| `triage/intent.py` | Taxonomy regexes: the reviewer's stated reason and revert tool from the revert summary, and the editor's declared intent from their own summary, each mapped onto `WIKI_REASONS` |
| `triage/features.py` | Diff parsing and save-time features, never anything known only after the save |
| `triage/edit_model.py` | Gradient boosting and TF-IDF logistic models, plus weighted metrics |
| `triage/edit_analysis.py` | The pandas tables |
| `triage/edit_eval.py` | The experiments |
| `triage/edit_plots.py` | The charts |

`notebooks/edit_intent.ipynb` holds the analysis and the answer. Its cells only call those
modules.

## Code quality gates

`ruff check`, `ruff format --check`, `pylint` (module size, 400 lines max) and
`pytest` run on every `lean-forge commit`. Keep modules small: one concern per
file under `triage/`.
