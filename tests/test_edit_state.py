"""The finality rules of PIPELINE.md section 7.4, over the log alone."""

from api_fakes import log_row

from triage import edit_state as es
from triage import steps
from triage.store import Store

E, C = steps.EXTRACT, steps.CLASSIFY
UNPARSEABLE = "unparseable reply: ValueError: no JSON"


def _final(store, kind, step, revid):
    first = log_row(store, kind, step, revid, error=UNPARSEABLE)
    log_row(store, kind, step, revid, error=UNPARSEABLE, extra={"retry_of": first})


def test_a_step_is_final_when_answered_or_when_its_retry_was_unparseable_too(tmp_path):
    store = Store(tmp_path / "calls.sqlite")
    log_row(store, "pipeline", E, 1, extra={"claims_added": []})
    log_row(store, "page", E, 2, error=UNPARSEABLE)  # the retry is still owed
    _final(store, "live", E, 3)
    log_row(store, "page", E, 4, error="HTTP 500: overloaded")
    log = es.read(store)
    assert [es.step_state(log, E, r) for r in (1, 2, 3, 4)] == [es.ANSWERED, es.OPEN, es.FAILED, es.OPEN]


def test_a_gateway_error_is_never_final_even_on_a_retry_row(tmp_path):
    store = Store(tmp_path / "calls.sqlite")
    first = log_row(store, "page", C, 5, error=UNPARSEABLE)
    log_row(store, "page", C, 5, error="RuntimeError: connection reset", extra={"retry_of": first})
    assert es.edit_state(es.read(store), 5) == es.OPEN


def test_a_legacy_pair_without_retry_of_is_final_within_one_run_only(tmp_path):
    store = Store(tmp_path / "calls.sqlite")
    log_row(store, "pipeline", E, 1, error=UNPARSEABLE)  # logged before `retry_of` existed
    log_row(store, "pipeline", E, 1, error=UNPARSEABLE)
    log_row(store, "pipeline", E, 2, error=UNPARSEABLE)  # one in each run: no pair
    log_row(store, "page", E, 2, error=UNPARSEABLE)
    first = log_row(store, "page", E, 3, error=UNPARSEABLE)  # a gateway error on the retry
    log_row(store, "page", E, 3, error="HTTP 500: overloaded", extra={"retry_of": first})
    log_row(store, "page", E, 3, error=UNPARSEABLE)  # a later first attempt, not a retry
    log = es.read(store)
    assert [es.step_state(log, E, r) for r in (1, 2, 3)] == [es.FAILED, es.OPEN, es.OPEN]


def test_an_answered_row_wins_over_a_final_error(tmp_path):
    store = Store(tmp_path / "calls.sqlite")
    _final(store, "page", C, 6)
    log_row(store, "live", C, 6, extra={"label": "content"})
    _final(store, "page", C, 6)  # an error after the answer does not undo it
    assert es.edit_state(es.read(store), 6) == es.CLASSIFIED


def test_probe_rows_never_count(tmp_path):
    store = Store(tmp_path / "calls.sqlite")
    log_row(store, "probe", E, 7, extra={"claims_added": []})
    log_row(store, "probe", C, 7, extra={"label": "content"})
    _final(store, "probe", E, 8)
    log = es.read(store)
    assert (es.edit_state(log, 7), es.edit_state(log, 8)) == (es.OPEN, es.OPEN)
    assert log.extracted == {} and log.classified == {} and log.failed == frozenset()


def test_an_edit_is_classified_errored_or_open(tmp_path):
    store = Store(tmp_path / "calls.sqlite")
    ext = log_row(store, "pipeline", E, 1, extra={"claims_added": []})
    store.log_rule_label(1, "maintenance", {"size_delta": 3}, ext)
    _final(store, "pipeline", E, 2)  # an errored extract: classify is never sent
    log_row(store, "pipeline", E, 3, extra={"claims_added": ["x"]})
    _final(store, "pipeline", C, 3)
    store.log_unavailable(4, "parent")
    log_row(store, "pipeline", E, 5, extra={"claims_added": ["x"]})  # classify still owed
    log = es.read(store)
    assert es.partition(log, [1, 2, 3, 4, 5, 6], pending={6}) == {
        es.CLASSIFIED: [1],
        es.ERRORED: [2, 3, 4],
        es.PENDING: [6],
        es.OPEN: [5],
    }
    assert es.errored_reason(log, 4) == es.WIKITEXT_UNAVAILABLE
    assert es.errored_reason(log, 2) == es.FINAL_ERROR
    assert store.rule_labels()[1]["extract_call_id"] == ext
    assert store.unavailable_diffs() == {4: "parent"}
