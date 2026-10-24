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
- Two task kinds share the harness: `github` (issue tagging, `triage/github.py`) and `wiki`
  (Wikipedia edit patrol keep/revert, `triage/wiki.py`, ground truth from the `mw-reverted`
  tag). `RepoConfig.kind` selects the fetcher and the frozen prompt template.
- `data/<repo>/` holds the committed dataset (labels and issues). `data/calls.sqlite` is the
  uncommitted evidence log. `data/guidelines/<repo>.md` accumulates maintainer corrections.
- Facts about the Mubit API that were verified by hand are in `MUBIT_API_NOTES.md`.
- Commits go through `lean-forge commit`; landing through `lean-forge land`.

- Live demo: `triage/live.py` (Wikimedia stream + stats, pure functions), `triage/patrol.py` (reader/worker threads, verdicts, corrections), `triage/server.py` (FastAPI, SSE at `/events`), `triage/static/patrol.html` (one page, no build step). Start with `uv run triage serve`.
- Local edit classifier (no model calls, never Mubit): `triage/corpus.py` + `corpus_build.py` (paced, cached Wikipedia API corpus under `data/corpus/`, `uv run triage corpus`), `triage/intent.py` (revert-reason / declared-intent regexes), `triage/features.py` (save-time features only), `triage/edit_model.py`, `edit_analysis.py`, `edit_eval.py`, `edit_plots.py`; analysis in `notebooks/edit_intent.ipynb` (thin cells).
- Gates: `uv run ruff check .`, `uv run ruff format --check .`, `uv run pylint triage tests` (400-line module cap), `uv run pytest -q` (needs node on the PATH: the extension tests fail, not skip, without it). Split a module before it reaches the cap.
- Watchlist pipeline (multi-step extract → classify → brief through Mubit, page analytics, Chrome extension, side-effect detection of Mubit switching): design, decisions and API contract in `PIPELINE.md`. Frozen step prompts live in `triage/steps.py`; treat them like `triage/prompt.py`.
