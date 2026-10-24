"""When an edit is done (PIPELINE.md section 7.4), as pure rules over the logged rows. Pipeline
resume, the classify endpoint's counts and PageAnalysis all use these, so there is one copy.

A step is final when it has an answered row (classify: or a rule label) or a final error, an
unparseable reply whose retry (the row with `retry_of` in its extra, or for rows logged before
that field, the next error row of the step in the same run) was unparseable too; an answered
row wins over error rows. A gateway error is never final. Probe rows never count."""

from __future__ import annotations

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field

from . import steps
from .provenance import error_group

PROBE = "probe"
NOT_PROBE = (PROBE,)  # Store.answered(skip_kinds=…) for normal runs and the brief
ANSWERED, FAILED, OPEN = "answered", "failed", "open"  # a step
CLASSIFIED, ERRORED, PENDING = "classified", "errored", "pending"  # an edit (or OPEN)
WIKITEXT_UNAVAILABLE = "wikitext unavailable (deleted or suppressed revision)"
FINAL_ERROR = "final error: an unparseable reply and its retry"


@dataclass(frozen=True)
class Log:
    """What the store knows about edits, probe rows left out."""

    extracted: dict[int, dict] = field(default_factory=dict)  # Store.answered of extract
    classified: dict[int, dict] = field(default_factory=dict)  # Store.answered of classify
    failed: frozenset[tuple[str, int]] = frozenset()  # (step, revid) with a final error
    rules: dict[int, dict] = field(default_factory=dict)  # Store.rule_labels
    unavailable: dict[int, str] = field(default_factory=dict)  # Store.unavailable_diffs


def final_errors(error_rows: Iterable[dict]) -> frozenset[tuple[str, int]]:
    """(step, revid) of every unparseable retry: its first attempt was unparseable too. Rows
    logged before `retry_of` existed name no first attempt, so there two unparseable rows in a
    row for the same step and revid in one run, neither with `retry_of`, are a final error too."""

    def first_attempt(r: dict | None) -> bool:  # an unparseable row that is not a retry
        return r is not None and error_group(r["error"]) == "unparseable" and "retry_of" not in r["extra"]

    out, last = set(), {}  # (run_id, step, revid) -> that key's previous error row
    for r in error_rows:
        key = (r["repo"], r["issue"])
        prev, last[(r["run_id"], *key)] = last.get((r["run_id"], *key)), r
        retry = "retry_of" in r["extra"] and error_group(r["error"]) == "unparseable"
        if retry or (first_attempt(r) and first_attempt(prev)):  # the second: a legacy retry
            out.add(key)
    return frozenset(out)


def read(store) -> Log:
    edit_steps = (steps.EXTRACT, steps.CLASSIFY)
    return Log(
        extracted=store.answered(steps.EXTRACT, skip_kinds=NOT_PROBE),
        classified=store.answered(steps.CLASSIFY, skip_kinds=NOT_PROBE),
        failed=final_errors(store.errors(edit_steps, skip_kinds=NOT_PROBE)),
        rules=store.rule_labels(),
        unavailable=store.unavailable_diffs(),
    )


def step_state(log: Log, step: str, revid: int) -> str:
    answered = log.extracted if step == steps.EXTRACT else log.classified
    if revid in answered or (step == steps.CLASSIFY and revid in log.rules):
        return ANSWERED
    return FAILED if (step, revid) in log.failed else OPEN


def edit_state(log: Log, revid: int) -> str:
    """CLASSIFIED, ERRORED (a final error in either step, or an unavailable diff) or OPEN."""
    if step_state(log, steps.CLASSIFY, revid) == ANSWERED:
        return CLASSIFIED
    if revid in log.unavailable or FAILED in (
        step_state(log, steps.EXTRACT, revid),
        step_state(log, steps.CLASSIFY, revid),
    ):
        return ERRORED
    return OPEN


def errored_reason(log: Log, revid: int) -> str:
    return WIKITEXT_UNAVAILABLE if revid in log.unavailable else FINAL_ERROR


def partition(log: Log, revids: Iterable[int], pending: Collection[int]) -> dict[str, list[int]]:
    """Each revid in exactly one of CLASSIFIED, ERRORED, PENDING (queued or running) and OPEN."""
    out: dict[str, list[int]] = {CLASSIFIED: [], ERRORED: [], PENDING: [], OPEN: []}
    for revid in revids:
        out[PENDING if revid in pending else edit_state(log, revid)].append(revid)
    return out
