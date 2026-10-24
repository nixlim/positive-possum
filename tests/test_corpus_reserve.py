"""corpus.Session's live reserve (PIPELINE.md sections 3 and 9)."""

import threading

import httpx
import pytest

from triage import corpus as cp

URL = "https://en.wikipedia.org/api/rest_v1/page/{}"


def _session(tmp_path, budget, reserve=0):
    s = cp.Session(tmp_path, budget, per_min=1e9, reserve=reserve)
    s.client = httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"path": r.url.path})),
        event_hooks={"request": [s._on_request]},
    )
    return s


def test_with_the_cli_defaults_the_session_exits_with_its_old_message(tmp_path):
    s = _session(tmp_path, 1)
    s.rest(URL.format(1))
    with pytest.raises(SystemExit, match=r"^request budget of 1 spent; re-run resumes from the cache$"):
        s.rest(URL.format(2))
    with pytest.raises(cp.BudgetSpent):
        s.rest(URL.format(3))
    assert s.sent.total() == 1  # a refused request is not counted


def test_only_requests_inside_using_reserve_spend_the_reserve(tmp_path):
    s = _session(tmp_path, 3, reserve=2)
    s.rest(URL.format(1))
    with pytest.raises(cp.BudgetSpent, match="reserve of 2"):
        s.rest(URL.format(2))
    with s.using_reserve():
        s.rest(URL.format(2))
        s.rest(URL.format(3))
        with pytest.raises(cp.BudgetSpent):  # nothing goes beyond the budget, reserve or not
            s.rest(URL.format(4))
    assert isinstance(cp.BudgetSpent("x"), SystemExit) and s.sent.total() == 3


def test_cache_hits_are_never_refused(tmp_path):
    s = _session(tmp_path, 2, reserve=1)
    assert s.rest(URL.format(1)) == {"path": "/api/rest_v1/page/1"}
    with pytest.raises(cp.BudgetSpent):
        s.rest(URL.format(2))
    assert s.rest(URL.format(1)) == {"path": "/api/rest_v1/page/1"} and s.hits == 1


def test_the_reserve_flag_belongs_to_the_thread_inside_using_reserve(tmp_path):
    s = _session(tmp_path, 2, reserve=2)
    inside, done = threading.Event(), threading.Event()
    got = {}

    def live():
        with s.using_reserve():
            inside.set()
            done.wait(10)
            got["live"] = s.rest(URL.format("live"))

    t = threading.Thread(target=live)
    t.start()
    assert inside.wait(10)
    with pytest.raises(cp.BudgetSpent):  # this thread is outside, whatever the other one does
        s.rest(URL.format("refresh"))
    done.set()
    t.join(10)
    assert not t.is_alive() and got["live"] and s.sent.total() == 1


def test_parallel_carries_the_reserve_into_its_worker_threads(tmp_path):
    s = _session(tmp_path, 3, reserve=3)
    with s.using_reserve():
        assert len(cp.parallel(s.rest, [URL.format(i) for i in range(3)])) == 3
    with pytest.raises(cp.BudgetSpent):
        cp.parallel(s.rest, [URL.format(9)])


def test_a_reserve_belongs_to_its_own_session(tmp_path):
    a, b = _session(tmp_path / "a", 1, reserve=1), _session(tmp_path / "b", 1, reserve=1)
    with a.using_reserve():
        a.rest(URL.format(1))
        with pytest.raises(cp.BudgetSpent):
            b.rest(URL.format(1))
