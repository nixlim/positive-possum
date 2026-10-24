"""The title index (PIPELINE.md section 9, "Titles")."""

import fcntl
import threading

import pytest
from test_pages import AS_OF, FakeSession, _page_route

from triage import pages as pg
from triage import titles


@pytest.mark.parametrize(
    ("asked", "want"),
    [
        ("Foo_bar", "Foo bar"),
        ("foo bar", "Foo bar"),
        ("Foo%20bar", "Foo bar"),
        ("  Foo \t  bar ", "Foo bar"),
        ("iPhone_SE", "IPhone SE"),
        ("Jos%C3%A9_Mar%C3%ADa", "José María"),
        ("", ""),
    ],
)
def test_titles_are_normalised_to_their_enwiki_form(asked, want):
    assert titles.normalise(asked) == want


def test_spellings_of_a_title_find_the_same_pageid(tmp_path):
    titles.record(tmp_path, {"Foo bar": 9})
    assert {titles.lookup(tmp_path, t) for t in ("Foo_bar", "foo bar", "Foo%20bar")} == {9}
    assert titles.lookup(tmp_path, "Foo baz") is None
    assert titles.load(tmp_path / "nothing") == {}


def test_two_concurrent_writers_lose_no_entry(tmp_path):
    writers, each = 8, 25
    start = threading.Barrier(writers, timeout=10)

    def write(w):
        start.wait()
        for i in range(each):
            titles.record(tmp_path, {f"Page {w}-{i}": w * 100 + i})

    threads = [threading.Thread(target=write, args=(w,)) for w in range(writers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads)
    assert len(titles.load(tmp_path)) == writers * each
    assert not list(tmp_path.glob("*.tmp"))


def test_the_writer_re_reads_under_the_exclusive_lock(tmp_path, monkeypatch):
    load, held = titles.load, []

    def load_under_lock(pages_dir):
        with open(pages_dir / titles.LOCK) as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another open file: it must wait
        held.append(True)
        return load(pages_dir)

    titles.record(tmp_path, {"A": 1})
    monkeypatch.setattr(titles, "load", load_under_lock)
    titles.record(tmp_path, {"B": 2})
    assert held == [True] and load(tmp_path) == {"A": 1, "B": 2}


def test_pages_ingest_records_the_asked_and_the_resolved_title(tmp_path):
    snaps = pg.ingest(FakeSession(_page_route), ["JD"], AS_OF, 90, tmp_path / "snapshots")
    assert [s["title"] for s in snaps] == ["Jane Doe"]
    assert titles.load(tmp_path) == {"JD": 5, "Jane Doe": 5}


def test_a_title_that_resolves_to_a_missing_page_is_not_recorded(tmp_path):
    def route(p):
        if p.get("prop") == "revisions" and "titles" in p:
            return {"query": {"pages": [{"title": p["titles"], "missing": True}]}}
        return _page_route(p)

    assert pg.ingest(FakeSession(route), ["Nobody"], AS_OF, 90, tmp_path / "snapshots") == []
    assert titles.load(tmp_path) == {}
