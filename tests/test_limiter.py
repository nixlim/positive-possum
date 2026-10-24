import threading
import time

from test_pipeline import CLASSIFY_OK, EXTRACT_OK, FakeMubit, _rev, _rows, _texts

from triage import pipeline, steps
from triage.caller import Caller, Limiter
from triage.store import Store


def test_a_shared_limiter_caps_every_caller_and_thread(tmp_path):
    limiter = Limiter(5)
    fake = FakeMubit({steps.EXTRACT: [EXTRACT_OK] * 4, steps.CLASSIFY: [CLASSIFY_OK] * 4})

    def work(kind, revids):  # a sqlite connection stays on the thread that opened it
        caller = Caller(fake, Store(tmp_path / "calls.sqlite"), kind, limiter=limiter)
        pipeline.run(caller, [_rev(i) for i in revids], _texts, log=lambda _m: None)

    jobs = (("pipeline", [101, 103]), ("live", [105, 107]))
    threads = [threading.Thread(target=work, args=a) for a in jobs]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads)
    assert len(fake.requests) == 5 and (limiter.spent, limiter.remaining) == (5, 0)
    kinds = {r["kind"] for r in _rows(Store(tmp_path / "calls.sqlite"))}
    assert kinds <= {"pipeline", "live"}

    hammered, took = Limiter(50), []

    def hammer():
        took.extend(hammered.take() for _ in range(20))

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads)
    assert sum(took) == 50 and hammered.remaining == 0


def test_a_limiter_paces_its_calls_and_callers_without_one_keep_their_own():
    paced, start = Limiter(3, rate=0.05), time.monotonic()
    assert all(paced.take() for _ in range(3)) and not paced.take()
    assert time.monotonic() - start >= 0.09
    a, b = Caller(None, None, "pipeline", 2), Caller(None, None, "pipeline", 2)
    assert a.limiter is not b.limiter and a.limiter.take()
    assert (a.left, b.left) == (1, 2)
    assert pipeline.Caller is Caller  # the pipeline's callers are these


def test_the_reserve_is_spent_only_by_live_callers(tmp_path):
    limiter = Limiter(4, reserve=2)
    assert limiter.take() and limiter.take() and not limiter.take()
    assert (limiter.spent, limiter.remaining, limiter.spendable) == (2, 2, 0)
    fake = FakeMubit({steps.EXTRACT: [EXTRACT_OK] * 2, steps.CLASSIFY: [CLASSIFY_OK] * 2})
    store = Store(tmp_path / "calls.sqlite")
    page = Caller(fake, store, "page", limiter=limiter)
    assert pipeline.run(page, [_rev(101)], _texts, log=lambda _m: None)[0]["error"] == "budget spent"
    assert page.refused and fake.requests == []
    live = Caller(fake, store, "live", limiter=limiter)
    [out] = pipeline.run(live, [_rev(101)], _texts, log=lambda _m: None)
    assert out["classification"]["label"] == CLASSIFY_OK["label"] and not live.refused
    assert limiter.remaining == 0 and not limiter.take(use_reserve=True)


def test_brief_through_a_spent_shared_caller_sends_nothing(tmp_path):
    fake = FakeMubit({steps.BRIEF: ["## What changed\nA fine was added."]})
    store = Store(tmp_path / "calls.sqlite")
    caller = Caller(fake, store, "brief", limiter=Limiter(0))
    analysis = {"pageid": 7, "title": "Acme", "risk_score": 0.4}
    assert pipeline.brief(fake, store, analysis, [], caller=caller) is None
    assert fake.requests == [] and _rows(store) == []
