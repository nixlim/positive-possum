from datetime import UTC, datetime

from triage import corpus as cp
from triage import pages as pg

AS_OF = datetime(2026, 10, 24, 14, tzinfo=UTC)


class FakeSession:
    """Answers Session.query from a routing function and records every request."""

    def __init__(self, route):
        self.route = route
        self.calls = []

    def query(self, params):
        self.calls.append(params)
        return self.route(params)


def _rev(revid, ts, user="Alice", size=1000, **kw):
    return {
        "revid": revid,
        "parentid": revid - 1,
        "timestamp": ts,
        "user": user,
        "userid": 7,
        "size": size,
        **kw,
    }


def test_fetch_window_follows_continuation_and_anchors_the_window():
    def route(p):
        if "rvcontinue" not in p:
            page = {"pageid": 5, "title": "Jane Doe", "revisions": [_rev(30, "2026-10-20T10:00:00Z")]}
            return {"continue": {"rvcontinue": "x", "continue": "||"}, "query": {"pages": [page]}}
        page = {"pageid": 5, "title": "Jane Doe", "revisions": [_rev(20, "2026-09-01T10:00:00Z")]}
        return {"query": {"pages": [page]}}

    s = FakeSession(route)
    page, revs = pg.fetch_window(s, "Jane_Doe", datetime(2026, 7, 26, 14, tzinfo=UTC), AS_OF)
    assert page == {"pageid": 5, "title": "Jane Doe"}
    assert [r["revid"] for r in revs] == [30, 20]
    assert s.calls[0]["rvstart"] == "2026-10-24T14:00:00Z"
    assert s.calls[0]["rvend"] == "2026-07-26T14:00:00Z"
    assert s.calls[1]["rvcontinue"] == "x"


def test_fetch_window_missing_page():
    s = FakeSession(lambda p: {"query": {"pages": [{"title": "Nobody", "missing": True}]}})
    assert pg.fetch_window(s, "Nobody", AS_OF, AS_OF) == (None, [])


def test_watchlist_is_anchored_and_continued():
    def route(p):
        members = [{"title": "B"}] if "cmcontinue" in p else [{"title": "A"}]
        cont = {} if "cmcontinue" in p else {"continue": {"cmcontinue": "c", "continue": "-||"}}
        return {**cont, "query": {"categorymembers": members}}

    s = FakeSession(route)
    assert pg.watchlist(s, pg.WATCHLISTS["mps"], AS_OF) == ["A", "B"]
    assert s.calls[0]["cmstart"] == "2026-10-24T14:00:00Z"
    assert s.calls[0]["cmtitle"] == "Category:UK MPs 2024–present"


def test_build_revisions_sizes_kinds_and_revert_tags():
    before = _rev(10, "2026-07-01T00:00:00Z", size=900)
    window = [
        _rev(13, "2026-10-03T00:00:00Z", user="~2025-1234-5", temp=True, size=1100, tags=["mw-undo"]),
        _rev(
            11, "2026-10-01T00:00:00Z", user="1.2.3.4", userid=0, anon=True, size=1000, tags=["mw-reverted"]
        ),
        _rev(12, "2026-10-02T00:00:00Z", size=950, minor=True, comment="Reverted edits by 1.2.3.4"),
        before,  # a revision exactly at the window start comes back from both queries
    ]
    revs = pg.build_revisions(window, before)
    assert [r["revid"] for r in revs] == [11, 12, 13]
    assert [r["size_delta"] for r in revs] == [100, -50, 150]
    assert [r["editor_kind"] for r in revs] == ["anonymous", "registered", "temporary"]
    assert [r["reverted"] for r in revs] == [True, False, False]
    assert [r["reverting"] for r in revs] == [False, True, True]
    assert revs[1]["minor"] and not revs[0]["minor"]


def test_build_revisions_new_page_counts_from_zero():
    revs = pg.build_revisions([_rev(1, "2026-10-01T00:00:00Z", size=400, parentid=0)], None)
    assert revs[0]["size_delta"] == 400


def test_fetch_users_with_groups_batches_and_skips_missing_and_invalid():
    def route(p):
        names = p["ususers"].split("|")
        users = [
            {"name": n, "editcount": 3, "registration": "2026-01-01T00:00:00Z", "groups": ["*"]}
            for n in names
        ]
        users[0] = {"name": names[0], "missing": True}
        return {"query": {"users": users}}

    s = FakeSession(route)
    names = [f"U{i:03d}" for i in range(60)]
    got = cp.fetch_users(s, names, groups=True)
    assert len(s.calls) == 2
    assert {c["usprop"] for c in s.calls} == {"editcount|registration|groups"}
    assert len(got) == 58 and "U000" not in got and "U050" not in got
    assert got["U001"] == {"registration": "2026-01-01T00:00:00Z", "editcount": 3, "groups": ["*"]}
    s = FakeSession(route)
    assert "groups" not in cp.fetch_users(s, names)["U001"]
    assert {c["usprop"] for c in s.calls} == {"editcount|registration"}  # the corpus cache key


def test_fetch_wikitext_batches_and_skips_hidden_revisions():
    def route(p):
        revids = [int(r) for r in p["revids"].split("|")]
        revs = [{"revid": r, "slots": {"main": {"content": f"text {r}"}}} for r in revids]
        revs[0] = {"revid": revids[0], "slots": {"main": {"texthidden": True}}}
        return {"query": {"pages": [{"pageid": 5, "revisions": revs}]}}

    s = FakeSession(route)
    got = pg.fetch_wikitext(s, [*range(1, 61), 3])
    assert len(s.calls) == 2 and s.calls[0]["rvslots"] == "main"
    assert len(got) == 58 and 1 not in got and 51 not in got
    assert got[2] == "text 2"


def test_drop_bots_removes_bot_accounts_and_their_edits():
    revs = pg.build_revisions(
        [_rev(1, "2026-10-01T00:00:00Z", user="FixBot"), _rev(2, "2026-10-02T00:00:00Z")], None
    )
    accounts = {"FixBot": {"groups": ["bot", "*"]}, "Alice": {"groups": ["*", "user"]}}
    kept, users = pg.drop_bots(revs, accounts)
    assert [r["revid"] for r in kept] == [2]
    assert list(users) == ["Alice"]


def test_top_accounts_skip_ip_editors():
    revs = [
        {"user": u, "editor_kind": k}
        for u, k in [("1.2.3.4", "anonymous")] * 5 + [("B", "registered")] * 2 + [("A", "registered")] * 2
    ]
    assert pg.top_accounts(revs, 5) == ["A", "B"]


def test_page_share_counts_this_page_in_the_last_contributions():
    contribs = [{"title": "Jane Doe"}] * 3 + [{"title": "Other"}]
    s = FakeSession(lambda p: {"query": {"usercontribs": contribs}})
    assert pg.page_share(s, "Alice", "Jane Doe", AS_OF) == 0.75
    assert s.calls[0]["uclimit"] == 50 and s.calls[0]["ucstart"] == "2026-10-24T14:00:00Z"
    assert pg.page_share(FakeSession(lambda p: {"query": {"usercontribs": []}}), "A", "X", AS_OF) is None


def test_count_words_drops_markup_and_keeps_link_text():
    text = (
        "<!-- note --> She won [[Bristol West|the seat]] in 2024.<ref name=a/>"
        '<ref name="b">{{cite web|title=Ignored words}}</ref> {{Infobox|x={{nested}}}}'
    )
    assert pg.count_words(text) == 6  # She won the seat in 2024


def test_split_sections_slices_at_utf8_byte_offsets():
    lead = "Zoë Ångström-Ćosić is a politician.\n"
    life = "== Early life ==\nBorn in Zürich.\n"
    crit = "=== <i>Expenses</i> ===\nShe repaid £1,000.\n"
    text = lead + life + crit
    sections = [
        {"line": "Early life", "level": "2", "byteoffset": len(lead.encode())},
        {"line": "From a template", "level": "2", "byteoffset": None},
        {"line": "<i>Expenses</i>", "level": "3", "byteoffset": len((lead + life).encode())},
    ]
    got = pg.split_sections(text, sections)
    assert got == [
        {"title": "(lead)", "level": 0, "words": 6},
        {"title": "Early life", "level": 2, "words": 3},
        {"title": "Expenses", "level": 3, "words": 4},
    ]


def _lead(revid):
    return f"Lead text for {revid}.\n"


def _page_route(p, hidden=()):
    if p.get("action") == "parse":
        assert p["oldid"] not in hidden  # parse on a hidden revision is an API error
        heads = [{"line": "Career", "level": "2", "byteoffset": len(_lead(p["oldid"]).encode())}]
        return {"parse": {"sections": heads}}
    if "revids" in p:
        revs = [
            {"revid": r, "slots": {"main": {"texthidden": True}}}
            if r in hidden
            else {"revid": r, "slots": {"main": {"content": _lead(r) + "== Career ==\nOne two three.\n"}}}
            for r in map(int, p["revids"].split("|"))
        ]
        return {"query": {"pages": [{"pageid": 5, "revisions": revs}]}}
    if p.get("list") == "users":
        return {
            "query": {
                "users": [
                    {
                        "name": "Alice",
                        "editcount": 50,
                        "registration": "2020-01-01T00:00:00Z",
                        "groups": ["*"],
                    },
                    {
                        "name": "TidyBot",
                        "editcount": 9,
                        "registration": "2019-01-01T00:00:00Z",
                        "groups": ["bot"],
                    },
                ]
            }
        }
    if p.get("list") == "usercontribs":
        return {"query": {"usercontribs": [{"title": "Jane Doe"}, {"title": "Other"}]}}
    if "pageids" in p:
        return {
            "query": {"pages": [{"pageid": 5, "revisions": [_rev(10, "2026-07-01T00:00:00Z", size=900)]}]}
        }
    revs = [
        _rev(12, "2026-10-02T00:00:00Z", user="TidyBot", size=1010),
        _rev(11, "2026-10-01T00:00:00Z", size=1000),
        _rev(13, "2026-10-03T00:00:00Z", user="9.9.9.9", userid=0, anon=True, size=990),
    ]
    return {"query": {"pages": [{"pageid": 5, "title": "Jane Doe", "revisions": revs}]}}


def test_ingest_page_builds_the_snapshot_and_round_trips(tmp_path):
    s = FakeSession(_page_route)
    snap = pg.ingest_page(s, "Jane Doe", AS_OF, 90)
    assert set(snap) == {
        "title",
        "pageid",
        "as_of",
        "days",
        "revisions",
        "users",
        "sections_then",
        "sections_now",
    }
    assert snap["as_of"] == "2026-10-24T14:00:00+00:00"
    assert [r["revid"] for r in snap["revisions"]] == [11, 13]  # the bot edit is gone
    assert [r["size_delta"] for r in snap["revisions"]] == [100, -20]  # deltas measured before dropping it
    assert snap["users"] == {
        "Alice": {"registration": "2020-01-01T00:00:00Z", "editcount": 50, "groups": ["*"], "page_share": 0.5}
    }
    users_call = next(c for c in s.calls if c.get("list") == "users")
    assert "9.9.9.9" not in users_call["ususers"]
    assert snap["sections_then"]["revid"] == 10
    assert snap["sections_now"]["revid"] == 13  # newest in the window, bot or not
    assert snap["sections_now"]["sections"] == [
        {"title": "(lead)", "level": 0, "words": 4},
        {"title": "Career", "level": 2, "words": 3},
    ]
    pg.write_snapshot(snap, tmp_path)
    assert pg.load_snapshot(5, tmp_path) == snap
    assert [p.name for p in tmp_path.iterdir()] == ["5.json.gz"]  # no temp file left behind


def test_a_hidden_revision_gives_null_sections_instead_of_ending_the_run():
    s = FakeSession(lambda p: _page_route(p, hidden={13}))
    snap = pg.ingest_page(s, "Jane Doe", AS_OF, 90)
    assert snap["sections_now"] is None
    assert snap["sections_then"]["revid"] == 10
    assert [c["oldid"] for c in s.calls if c.get("action") == "parse"] == [10]
