# Mubit triage

A Mubit Night Shift hackathon project (23–25 Oct 2026). It sends real classification work
through [Mubit](https://mubit.ai) and measures, from outside, when Mubit's task-adapted model
starts answering in place of the provider model.

Mubit is a recording proxy in front of the model providers. It groups calls into tasks (same
model, system prompt and output schema), keeps the examples its own agent judges good, trains a
small adapter per task and at some point routes the task to it. It never says when: there is no
read API, and a caller cannot ask for a specific adapter. So every call this repo makes is logged
with what came back (served model string, response headers, reply shape, latency, tokens), and
the switch is detected from those side effects.

Every model call goes through Mubit, as the hackathon requires. There is no direct provider route
in this code base, and the Mubit key is the only credential the code ever sends.

## What is in the repo

| Part | What it does | Model calls |
| --- | --- | --- |
| [Triage harness](#triage-harness) | Tags GitHub issues (Godot, Flutter) and patrols Wikipedia edits (keep/revert); scheduled evals against ground truth and a provider-served reference run | Mubit |
| [Patrol desk](#patrol-desk) | Live web page classifying the Wikipedia recent-changes stream as it arrives | Mubit |
| [Local edit classifier](#local-edit-classifier) | A corpus of real enwiki edits and a notebook asking whether edits can be classified by intention | None |
| [Watchlist pages](#watchlist-pages) | Snapshots of watched articles (UK MPs) and page-level signals: bursts, concentration, new accounts, reverts, structure drift, bloat, risk score | None |
| [Seeing the switch](#seeing-the-switch) | Per-step provenance: did anything about who answers change? | None (reads the log) |
| [Chrome extension](#chrome-extension) | A page's signals on the Wikipedia article itself: toolbar badge, banner and drawer | None (reads the local API) |
| [Watchlist pipeline](#watchlist-pipeline) | Multi-step extract → classify → brief through Mubit, with the API and dashboard the extension reads | Mubit (being built) |

The design, the decisions taken on 24 Oct and the API contract of the watchlist work are in
[`PIPELINE.md`](PIPELINE.md). Facts about the Mubit API checked by hand are in
[`MUBIT_API_NOTES.md`](MUBIT_API_NOTES.md).

## Setup

```
cp .env.example .env     # MUBIT_API_KEY=mbt_...; optional TRIAGE_MODEL, GITHUB_TOKEN
uv sync
uv run pytest -q
```

GitHub access uses `GITHUB_TOKEN` or the logged-in `gh` CLI. Wikipedia needs no key, only the
User-Agent the code sends.

The shell profile on this machine exports `ANTHROPIC_BASE_URL` and `ANTHROPIC_CUSTOM_HEADERS`
for other tools. The client ignores the base URL but refuses to start while the custom headers
variable is set, so clear both before any command that calls Mubit:

```
unset ANTHROPIC_CUSTOM_HEADERS ANTHROPIC_BASE_URL
set -a; . ./.env; set +a
```

All model calls use Mubit's Anthropic route with `claude-sonnet-5-5`. Every call, failures
included, is written to `data/calls.sqlite` (uncommitted) before anything uses it: the response
headers, the served model string, the Mubit request id (`x-middleware-request-id`), latency and
tokens. `TRIAGE_DATA` points the code at another data directory.

## Rules the code keeps

- **Frozen task prompts.** Mubit identifies a task by model + system prompt + output schema.
  Changing one word forks a new task and resets its kept-example count. The prompts live in
  `triage/prompt.py`, guarded by golden-hash tests; the watchlist pipeline's step prompts follow
  the same rule. Anything that varies per call goes in the user message.
- **One session per item.** `metadata.user_id` is the item (`godot#124553`, `enwiki#<revid>`), so
  a re-send lands beside the original answer and Mubit's held-out test cases stay disjoint.
- **Polite Wikipedia access.** At most 160 requests a minute, 3 at a time, `maxlag=5`,
  `Retry-After` honoured on 429, 503 and maxlag, a request budget per run, and a response cache,
  so a re-run sends nothing.
- **Signals, not findings.** Nothing here accuses anyone. Labels such as whitewash or smear are
  unscored signals for a human reviewer, and prompts describe edits, never the people who made
  them.

## Triage harness

```
uv run triage pull --repo godot --n 300        # data/godot/{labels.json,issues.jsonl}
uv run triage eval --repo godot                 # through Mubit; the first provider-served run is the reference
uv run triage replay --repo godot --limit 60    # push training traffic through Mubit
uv run triage correct --repo godot --issue 124553 --labels bug,topic:animation  # train issues only
uv run triage watch --every 3600               # hourly Mubit evals for every repo with a dataset
uv run triage report                            # timeline: accuracy, agreement, latency, served-by
```

A fixed eval set is re-run through Mubit on a schedule and scored against the maintainers' real
labels, and compared with a reference run: the first run in which every reply was served by the
provider itself, checked against the served model string of each call. A change in the served
model, or a drop in agreement with the reference below the floor between two provider runs, is the
routing signal.

**Wikipedia edit patrol (`--repo enwiki`).** Given a diff, the edit summary and the editor type,
decide `keep` or `revert`, with a reason and a confidence. Ground truth is the community's own
verdict: the MediaWiki `mw-reverted` tag on edits 12 hours to 29 days old.
`triage pull --repo enwiki --n 200` fetches a balanced set (half reverted, half kept) with
`revid` as the item number; the same `eval`, `replay`, `correct`, `watch` and `report` commands
apply. It has its own frozen system prompt (`WIKI_SYSTEM_TEMPLATE`), so it is a separate Mubit
task.

Points about the method:

- Maintainer corrections go into a guidelines block in the user message
  (`data/guidelines/<repo>.md`), so they do not fork the task.
- Eval calls through Mubit are recorded and count toward the task. Nothing leaks: the recorded
  reply is the model's answer, not the ground truth. But once the adapter serves, it was trained
  on the exact eval inputs paired with the provider's answers, so `agreement_vs_reference` is
  biased upward and a drop understates the switch. Read it together with the served model and
  accuracy against the real labels.
- The reference run must share the task (`task_sha`), guidelines and model, and every answered
  call in it must have been served by the requested model (date suffix allowed). Without one a run
  gets `reference_mismatch` with the reason. Run the eval twice on the first night to learn the
  agreement floor between two runs of the provider itself.
- The enwiki task was forked on purpose on 2026-10-23 when reason and confidence were added
  ("Deliberate task forks" in `MUBIT_API_NOTES.md`).
- `uv run python mubit_smoke.py` re-checks by hand the API facts the request shapes rely on.

## Patrol desk

```
uv run triage serve --repo enwiki --rate 2     # http://127.0.0.1:8321
```

One page, three zones. Left: edits from the Wikimedia recent-changes stream, each classified
through Mubit as it arrives (verdict, reason, confidence, served model, latency, tokens). Centre:
the share of answers served by something other than the task model, p50 latency per served model,
and the frontier cost per day projected from logged tokens. Right: Keep / Revert. A click that
disagrees with the model appends one guideline line and re-sends the same edit in the same Mubit
session; a click that agrees records nothing. A reconcile job later checks what Wikipedia did
with each edit (`triage reconcile`).

The desk spends Mubit calls at the stream's pace (about one edit every two seconds at the default
rate), so watch the call cap. It has no authentication, and a verdict writes to
`data/guidelines/enwiki.md`, so `serve` refuses a non-loopback `--host` unless you also pass
`--allow-remote`.

## Local edit classifier

Can edits be classified by intention, without a language model? Nothing here calls Mubit,
Anthropic or any other model.

```
uv run triage corpus --windows 24 --diffs 10000     # about 15k Wikipedia API requests, cached
uv run jupyter nbconvert --to notebook --execute --inplace notebooks/edit_intent.ipynb
```

`triage corpus` samples 24 one-hour windows over days 4 to 27 before today, lists every article
edit in them, links each `mw-reverted` edit to the edit that reverted it (summary, tool, delay),
fetches diffs for the reverted human edits and a uniform sample of kept ones, then the editors'
edit counts, and scores up to 400 test-window edits with Wikimedia's keyless
`revertrisk-language-agnostic` model on Lift Wing. The live desk's edits are included, so the
local model can be set against the logged Sonnet verdicts. Everything is cached under
`data/corpus/` (gitignored); the anchor date is in `data/corpus/manifest.json`.

| Module | Job |
| --- | --- |
| `triage/corpus.py` | Sampling and linking helpers, and the paced, cached Wikipedia session |
| `triage/corpus_build.py` | Runs the build and loads the corpus |
| `triage/intent.py` | Reviewer's stated reason and revert tool from the revert summary; editor's declared intent from their own summary |
| `triage/features.py` | Diff parsing and save-time features, never anything known only after the save |
| `triage/edit_model.py` | Gradient boosting and TF-IDF logistic models, weighted metrics |
| `triage/edit_analysis.py`, `edit_eval.py`, `edit_plots.py` | Tables, experiments, charts |

`notebooks/edit_intent.ipynb` holds the analysis and the answer; its cells only call these
modules.

## Watchlist pages

```
uv run triage pages candidates --titles "Keir Starmer|Diane Abbott|Nigel Farage"
uv run triage pages candidates --watchlist mps --top 25    # Category:UK MPs 2024–present
uv run triage pages ingest --watchlist mps --days 90 --as-of 2026-10-24T14:00Z
```

`triage pages ingest` writes one snapshot per article to `data/pages/snapshots/<pageid>.json.gz`
(gitignored): the human revisions of an anchored 90-day window with size deltas and revert tags,
the accounts that made them (registration, edit count, groups; bots and their edits dropped),
each top account's share of its last 50 contributions spent on the page, and the article's
sections at the start and end of the window. `triage pages candidates` also ranks the pages for
the demo and writes `data/pages/candidates.json` with a reason per page. No model is called;
`--budget` caps the requests sent.

`triage/page_analytics.py` turns a snapshot into page-level signals, all pure functions:

- edits per day and burst days (more than mean + 2σ and at least 3);
- the top-3 accounts' share of edits, single-purpose accounts, new accounts (registered under
  30 days before the edit), IP and temporary-account shares, reverts;
- structure drift: where the controversy section sits then and now (headings matching
  criticism, legal issues, allegations, scandal, investigation, expenses and similar), and bloat:
  growth outside the controversy sections relative to the old article, next to the controversy
  share of all words;
- `risk_score` with its parts, documented in `PIPELINE.md` section 5. The flagged-edit term
  stays partial until the pipeline has classified the window's edits.

## Seeing the switch

```
uv run triage provenance --hours 24            # per step; --step edit-classify, --json
```

`triage provenance` reads the call log and asks, per step, whether anything about who answers
changed. A provider-served call shows the asked model as the served string, an
`anthropic-organization-id` header and a `req_` request id; Mubit's own model is not expected to.
The baseline is a step's first 200 provider-served answers. A recent answer that fails the
provider test, or brings a new reply id prefix, usage key set or rate-limit header presence, is a
hard signal. Latency or output length moving outside the baseline's interquartile range is a soft
one. Errors are split into gateway and unparseable and never scored. Repeat runs of fixed edits
(`kind = probe`) give output agreement with the first answers (`triage/agreement.py`).

As of 24 Oct 2026, 15:10 UTC, every answered call passes the provider test (godot 512, flutter
600, enwiki 1,103; enwiki's 4 errors are unparseable replies) and no step shows a signal: Mubit
has not yet routed any of them. The console's Requests page ("Served by") is the confirmation.

## Chrome extension

`extension/` is a Manifest V3 extension in plain JavaScript, with no build step. Load it from
`chrome://extensions`: turn on Developer mode, choose **Load unpacked** and pick the
`extension/` directory. On an English Wikipedia article it shows:

- a toolbar badge coloured by the page's risk score, with the number of flagged edits in the
  last 30 days;
- a banner under the article title;
- a drawer with the tabs Pulse (edits per day, bursts), Flags (classified edits, forced reviews),
  Editors, Structure (controversy section and bloat), Brief and Mubit (error groups, switch
  signals, repeat-run agreement), and a footer saying per step whether the provider answered.

It reads the local API at `http://127.0.0.1:8322`. Until that API runs, or with demo mode on in
the options page, it shows bundled fixtures about a synthetic article, marked "offline". Only two
actions spend anything: requesting a brief (Mubit calls) and refreshing a page from Wikipedia.
Details are in [`extension/README.md`](extension/README.md).

## Watchlist pipeline

Being built in units (`PIPELINE.md` section 12):

- three Mubit tasks: `edit-extract` (claims added and removed, negative information, tone),
  `edit-classify` (one label, severity, review flag) and `page-brief` (a short hedged brief), with
  everything measurable (words, citations, sections) counted in code and the quoted article text
  fenced as data;
- `triage/api.py` on http://127.0.0.1:8322, which the extension reads, with a watchlist
  dashboard.

## Repository layout

```
triage/            the package (one concern per module, 400 lines at most)
  prompt.py        frozen triage prompts
  llm.py           the Mubit client           store.py      the call log (data/calls.sqlite)
  github.py, wiki.py                          fetchers
  runner.py, metrics.py, report.py            evals and the timeline
  live.py, patrol.py, server.py, static/      the patrol desk
  corpus*.py, intent.py, features.py, edit_*.py   the local edit classifier
  pages.py, page_analytics.py, candidates.py  watchlist pages
  provenance.py, agreement.py                 seeing the switch
extension/         the Chrome extension
tests/             pytest, no network
notebooks/         edit_intent.ipynb
data/              committed datasets per repo; calls.sqlite, corpus/ and pages/ stay local
```

## Development

The gates are `uv run ruff check .`, `uv run ruff format --check .`, `uv run pylint triage tests`
(module size, 400 lines at most) and `uv run pytest -q`. Changes are committed with
`lean-forge commit`, which runs the gates and an independent review, and landed with
`lean-forge land`; nothing is committed or pushed by hand.
