# Wikipedia EditWatch

**Wikipedia EditWatch for Misinformation Risk Intelligence.** A Chrome extension and the local
backend behind it, built for the Mubit Night Shift hackathon (23–25 Oct 2026).

Open any English Wikipedia article and EditWatch shows how the article has been edited lately:
how much, by whom, what the edits changed, and whether the pattern is worth a closer look. The
language-model work (reading and classifying edits, writing a brief) runs through
[Mubit](https://mubit.ai).

## Why

Misinformation on Wikipedia rarely arrives as one obvious edit. It looks like a pattern: a
burst of edits from new or single-purpose accounts, sourced criticism softened or moved down the
page, unsourced claims added to a biography. Patrollers see edits one at a time; readers see
only the result. EditWatch puts the page-level picture on the article itself, so a reader or an
editor can tell whether the article is being worked on and what to check.

Everything EditWatch shows is a signal for human review, not a finding. It describes edits and
accounts, never the motives or identity of people.

## What the demo shows

1. **Open an article.** On the first visit EditWatch reads the article's last 90 days of
   history from Wikipedia (no model call). A banner appears under the title and the toolbar
   badge shows the number of flagged edits. The banner or the toolbar icon opens a drawer with
   six tabs.
2. **Pulse**: edits per day and burst days.
3. **Flags → Classify recent edits.** Each recent edit goes through two Mubit steps: *extract*
   (claims added and removed, what happened to negative information, tone shift) and *classify*
   (one label: content, maintenance, sourcing, promotional, unsourced claim, whitewash, smear,
   vandalism or unclear; a severity from 0 to 3; and whether a human should review it). The
   banner counts down the edits left, then the flagged edits are listed with a one-line
   rationale each.
4. **Editors**: who edits the page: the top accounts' share, new accounts, single-purpose
   accounts, IP and temporary accounts, reverts.
5. **Structure**: whether the controversy section moved or shrank, and how much the article
   grew elsewhere ("bloat").
6. **Brief → Request brief.** A third Mubit step writes a hedged brief of at most 180 words:
   What changed, Who, Pattern, Why now, Suggested checks.
7. **Mubit**: for each step, who answered (the provider model or Mubit's own), switch signals
   and error counts.

## Risk score

A number from 0 to 100, added up from six measured signals:

| Signal | Points |
| --- | --- |
| Edits flagged for review in the last 30 days (5 or more scores in full) | 30 |
| Share of edits made by the three most active accounts | 20 |
| Controversy section moved down the page or removed | 15 |
| Reverted edits in the last 30 days (3 or more scores in full) | 15 |
| Share of edits by accounts less than 30 days old | 10 |
| Any single-purpose account editing the page | 10 |

The badge is green below 30, amber from 30 to 60 and red above 60. It stays grey until the
recent edits are classified, because the flagged count would otherwise understate the score.
The weights are fixed by hand, not tuned to the demo pages. The score is a prompt to look, not a
verdict. The formula is in `PIPELINE.md` section 5.

## Mubit, and what we could not show

Mubit is a recording proxy in front of the model providers. It groups calls into tasks (same
model, system prompt and output schema), and once a task has enough good examples it trains a
small task-adapted model and starts answering with it. EditWatch's three steps are narrow,
repeated tasks of that kind, which would make classifying every edit cheap. Every call goes
through Mubit's Anthropic route with `claude-sonnet-5-5`, and each one is logged with the served
model, response headers, latency and tokens, so a switch to Mubit's model can be seen from
outside.

We were hit by outages during the event, and Mubit's training of the three tasks did not
complete in time. Every answer EditWatch logged was served by `claude-sonnet-5-5`, so we could
not compare Mubit's own model with the provider on real data. The Mubit tab and the
`triage provenance` command are ready to show the switch when it happens.

## Run EditWatch

You need [uv](https://docs.astral.sh/uv/), Chrome, and a Mubit key.

1. **Install.**

   ```
   cp .env.example .env     # set MUBIT_API_KEY=mbt_...
   uv sync
   ```

   The Mubit key is the only credential the code sends. If your shell exports
   `ANTHROPIC_CUSTOM_HEADERS` or `ANTHROPIC_BASE_URL`, unset them first: the client refuses to
   start while the custom headers variable is set.

2. **Load the extension.** Open `chrome://extensions`, turn on Developer mode, choose
   **Load unpacked** and pick the `extension/` directory. Copy the extension's ID.

3. **Start the backend** with that ID, so the API accepts the extension's requests:

   ```
   uv run triage api --extension-origin chrome-extension://<id>
   ```

   It serves http://127.0.0.1:8322 on loopback only. Without the ID, Classify, Request brief
   and the first-visit read get `403`.

4. **Open an article** on `https://en.wikipedia.org/wiki/…`. Reading the history costs only
   Wikipedia requests. Only **Classify recent edits** (at most four Mubit calls per edit,
   retries included) and **Request brief** spend Mubit calls.

The API caps what one process can spend: `--call-budget` (300 Mubit calls) and
`--request-budget` (2,000 Wikipedia requests), each with a reserve kept for the live feed. A
spent budget returns `429` and the extension says so. In Options, demo mode shows bundled
fixture data about a synthetic article, with no backend needed.

The backend also serves a dashboard at http://127.0.0.1:8322/: watchlist pages (UK MPs by
default) ranked by risk score, and a live feed that classifies new edits to them as they arrive.
It lists only pages already read; `uv run triage pages ingest --watchlist mps` fills it (no
Mubit calls). `--no-live` turns the live feed off.

Command-line equivalents of the extension's buttons:

```
uv run triage pages ingest --titles "Ed Davey"                   # read a page's history
uv run triage pipeline run --titles "Ed Davey" --max-calls 20    # classify its edits
uv run triage pipeline brief --title "Ed Davey"                  # write its brief
uv run triage provenance --hours 24                              # who answered, per step
```

## Develop

```
uv run ruff check .
uv run ruff format --check .
uv run pylint triage tests       # includes a 400-line cap per module
uv run pytest -q                 # needs node on the PATH: the extension tests fail without it
```

- **Prompts are frozen.** Mubit identifies a task by model, system prompt and output schema;
  changing one word starts a new task with no examples. The step prompts are in
  `triage/steps.py`, guarded by golden-hash tests. Anything that varies per call goes in the
  user message.
- **Measured, not asked.** Words, citations, sections and every risk-score term are counted in
  code. The model only reads and judges the edit, and quoted article text is fenced as data.
- **Polite to Wikipedia.** Requests are paced, budgeted and cached, so a re-run sends nothing.
- **Tests make no network calls.** Extension tests run the service worker and content scripts
  under node against stubbed Chrome and DOM APIs.

**Built with lean-forge.** EditWatch was developed with lean-forge, a Claude Code plugin that runs
agent-written changes through a mechanical gate chain. `lean-forge commit` runs the four gates
above, a secrets scan and an independent reviewer in a fresh context, with a capped fix loop, and
commits exactly the tree that was reviewed. `lean-forge land` rebases onto `main` under a lock,
re-runs the gates and fast-forward pushes. The gates, and the review focus (frozen prompts, Mubit
as the only egress, every call logged), are in `.lean-forge/config.toml`. Nothing is committed or
pushed by hand.

| Where | What |
| --- | --- |
| `extension/` | The Chrome extension, plain JavaScript, no build step ([details](extension/README.md)) |
| `triage/api.py`, `watch*.py`, `classify_queue.py`, `static/watch.html` | The API and dashboard |
| `triage/steps.py`, `pipeline.py`, `caller.py`, `classification.py` | The three Mubit steps |
| `triage/pages.py`, `page_analytics.py` | Page history and signals, risk score |
| `triage/provenance.py`, `agreement.py` | Who answered, per step |
| `PIPELINE.md` | Design, decisions and the API contract |
| `MUBIT_API_NOTES.md` | Facts about the Mubit API checked by hand |

## Also in this repo

Earlier work from the same weekend that EditWatch grew out of:

- **Patrol desk** (`uv run triage serve --repo enwiki`, http://127.0.0.1:8321): our first test
  bed. It classifies the live Wikipedia recent-changes stream through Mubit as keep or revert,
  and lets a person agree or correct.
- **Triage harness** (`triage eval|replay|watch|report`): tags GitHub issues for Godot and
  Flutter, and judges Wikipedia edits as keep or revert, through Mubit. It scores the answers
  against real labels and against the provider's first answers, to catch a switch.
- **Local edit classifier** (`triage corpus`, `notebooks/edit_intent.ipynb`): a corpus of real
  Wikipedia edits and a notebook testing whether edits can be classified by intent without a
  language model.
