"""pipeline.run against the finality rules: rule labels, unavailable diffs, final errors and
resume (PIPELINE.md section 7.4)."""

from functools import partial

import httpx
import pytest
from test_pages import FakeSession
from test_pipeline import CLASSIFY_OK, EMPTY_EXTRACT, EXTRACT_OK, NEW, OLD, FakeMubit, _rev, _rows, _run

from triage import edit_state as es
from triage import pages, steps

E, C = steps.EXTRACT, steps.CLASSIFY


def _steps(fake):
    return [next(s for s, p in steps.SYSTEMS.items() if p == r["system"]) for r in fake.requests]


def test_a_rule_label_is_stored_and_never_re_sent(tmp_path):
    fake = FakeMubit({E: [EMPTY_EXTRACT]})
    [out], store = _run(tmp_path, fake, [_rev(11, size_delta=5)])
    assert out["classification"]["source"] == "rule" and len(fake.requests) == 1
    [rule] = store.rule_labels().values()
    assert (rule["label"], rule["measured"]["size_delta"]) == ("maintenance", 5)
    assert rule["extract_call_id"] == _rows(store)[0]["id"]
    again = FakeMubit({})
    [out], _ = _run(tmp_path, again, [_rev(11, size_delta=5)])
    assert again.requests == [] and out["classification"]["source"] == "rule"


@pytest.mark.parametrize(("texts", "missing"), [({}, "revision"), ({11: NEW}, "parent")])
def test_an_unavailable_diff_is_recorded_makes_no_call_and_is_not_fetched_again(tmp_path, texts, missing):
    fake = FakeMubit({})
    [out], store = _run(tmp_path, fake, [_rev(11)], texts=lambda _ids: texts)
    assert fake.requests == [] and out["error"] == es.WIKITEXT_UNAVAILABLE
    assert store.unavailable_diffs() == {11: missing}
    fetched = []
    [out], _ = _run(tmp_path, fake, [_rev(11)], texts=lambda ids: fetched.append(ids) or {})
    assert fake.requests == [] and all(not ids for ids in fetched)
    assert out["error"] == es.WIKITEXT_UNAVAILABLE


def test_a_revision_left_for_a_continuation_is_not_recorded_as_unavailable(tmp_path):
    def route(p):  # the first reply hit the result-size limit and left out the parent, 10
        rev, text = (10, OLD) if "rvcontinue" in p else (11, NEW)
        cont = {} if "rvcontinue" in p else {"continue": {"rvcontinue": "10", "continue": "||"}}
        revs = [{"revid": rev, "slots": {"main": {"content": text}}}]
        return {**cont, "query": {"pages": [{"pageid": 5, "revisions": revs}]}}

    fake = FakeMubit({E: [EXTRACT_OK], C: [CLASSIFY_OK]})
    [out], store = _run(tmp_path, fake, [_rev(11)], texts=partial(pages.fetch_wikitext, FakeSession(route)))
    assert out["error"] is None and _steps(fake) == [E, C] and store.unavailable_diffs() == {}


def test_a_probe_records_no_unavailable_diff(tmp_path):
    fake = FakeMubit({})
    [out], store = _run(tmp_path, fake, [_rev(11)], texts=lambda _ids: {}, kind="probe")
    assert fake.requests == [] and out["error"] == es.WIKITEXT_UNAVAILABLE
    assert store.unavailable_diffs() == {} and es.edit_state(es.read(store), 11) == es.OPEN


def test_a_first_revision_diffs_against_empty_text(tmp_path):
    fake = FakeMubit({E: [EXTRACT_OK], C: [CLASSIFY_OK]})
    [out], store = _run(tmp_path, fake, [_rev(11, parentid=0)], texts=lambda _ids: {11: NEW})
    assert out["error"] is None and _steps(fake) == [E, C] and store.unavailable_diffs() == {}


def test_a_failed_fetch_records_nothing_and_leaves_the_edit_open(tmp_path):
    def fail(_ids):
        raise httpx.ConnectError("refused")

    with pytest.raises(httpx.ConnectError):
        _run(tmp_path, FakeMubit({}), [_rev(11)], texts=fail)
    _, store = _run(tmp_path, FakeMubit({E: [EXTRACT_OK], C: [CLASSIFY_OK]}), [])
    assert store.unavailable_diffs() == {} and es.edit_state(es.read(store), 11) == es.OPEN


def test_an_edit_costs_at_most_four_calls_and_a_final_error_is_never_re_sent(tmp_path):
    fake = FakeMubit({E: ["not json", EXTRACT_OK], C: ["not json", "still not json"]})
    [out], store = _run(tmp_path, fake, [_rev(11)])
    assert _steps(fake) == [E, E, C, C] and out["error"].startswith("unparseable reply")
    assert es.edit_state(es.read(store), 11) == es.ERRORED
    again = FakeMubit({})
    [out], _ = _run(tmp_path, again, [_rev(11)])
    assert again.requests == [] and out["error"] == es.FINAL_ERROR


def test_a_gateway_error_leaves_the_step_open_for_the_next_run(tmp_path):
    fake = FakeMubit({E: [RuntimeError("connection reset")]})
    [out], store = _run(tmp_path, fake, [_rev(11)])
    assert len(fake.requests) == 1 and out["error"].startswith("RuntimeError")
    assert es.edit_state(es.read(store), 11) == es.OPEN
    again = FakeMubit({E: [EXTRACT_OK], C: [CLASSIFY_OK]})
    [out], _ = _run(tmp_path, again, [_rev(11)])
    assert _steps(again) == [E, C] and out["classification"]["label"] == "content"
