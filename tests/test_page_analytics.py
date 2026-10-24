from datetime import UTC, datetime, timedelta

from triage import page_analytics as pa
from triage.wiki import EDITOR_LABELS

AS_OF = datetime(2026, 10, 24, 12, tzinfo=UTC)


def rev(revid, days_ago, user="Alice", kind="registered", reverted=False):
    ts = (AS_OF - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"revid": revid, "timestamp": ts, "user": user, "editor_kind": kind, "reverted": reverted}


def sec(title, level, words):
    return {"title": title, "level": level, "words": words}


def account(registered_days_ago, editcount=100, page_share=None):
    reg = (AS_OF - timedelta(days=registered_days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"registration": reg, "editcount": editcount, "groups": ["*"], "page_share": page_share}


def snapshot(revisions, users=None, then=None, now=None):
    return {
        "title": "Jane Doe",
        "pageid": 5,
        "as_of": AS_OF.isoformat(),
        "days": 90,
        "revisions": revisions,
        "users": users or {},
        "sections_then": {"revid": 1, "timestamp": "x", "sections": then} if then is not None else None,
        "sections_now": {"revid": 2, "timestamp": "y", "sections": now or [sec("(lead)", 0, 10)]},
    }


def test_edits_per_day_covers_the_whole_window_with_zero_days():
    per_day = pa.edits_per_day([rev(1, 0), rev(2, 0), rev(3, 5)], AS_OF, 90)
    assert len(per_day) == 91
    assert per_day[0] == {"date": "2026-07-26", "count": 0}
    assert per_day[-1] == {"date": "2026-10-24", "count": 2}
    assert sum(d["count"] for d in per_day) == 3


def test_burst_days_need_two_sigma_and_at_least_three_edits():
    quiet = [{"date": f"d{i:02d}", "count": i % 2} for i in range(30)]
    assert pa.burst_days([*quiet, {"date": "spike", "count": 6}]) == ["spike"]
    small = [{"date": f"d{i:02d}", "count": 0} for i in range(30)] + [{"date": "two", "count": 2}]
    assert pa.burst_days(small) == []
    assert pa.burst_days([]) == []


def test_top3_share_counts_each_ip_as_its_own_editor():
    revs = [rev(i, 1, u) for i, u in enumerate("AAAAABBBCD")]
    assert pa.top_share(revs) == 0.9
    assert pa.top_share([]) == 0.0


def test_spa_needs_ten_edits_here_and_most_recent_contribs_on_the_page():
    assert pa.is_spa(10, account(400, page_share=0.6))
    assert not pa.is_spa(9, account(400, page_share=0.9))
    assert not pa.is_spa(12, account(400, page_share=0.5))
    assert not pa.is_spa(12, account(400))  # not one of the accounts usercontribs was asked about
    assert not pa.is_spa(12, None)


def test_new_account_share_counts_young_registered_accounts_only():
    users = {"Newbie": account(10), "Veteran": account(4000)}
    revs = [
        rev(1, 5, "Newbie"),
        rev(2, 5, "Veteran"),
        rev(3, 5, "1.2.3.4", "anonymous"),
        rev(4, 5, "~2026-1", "temporary"),
    ]
    a = pa.activity(snapshot(revs, users))
    assert a["new_account_share"] == 0.25
    assert a["ip_share"] == 0.25 and a["temp_share"] == 0.25
    # registered 40 days before as_of, so 10 days old when it edited 30 days ago
    assert pa.is_new_account(rev(5, 30, "Recent"), {"Recent": account(40)})
    assert not pa.is_new_account(rev(5, 1, "Recent"), {"Recent": account(40)})


def test_activity_windows_reverts_and_flags():
    revs = [rev(1, 60, reverted=True), rev(2, 20, reverted=True), rev(3, 2), rev(4, 1)]
    edits = {
        2: {"classification": {"severity": 2, "review": False}},
        3: {"classification": {"severity": 0, "review": True}},
        4: {"classification": {"severity": 1, "review": False}},
    }
    a = pa.activity(snapshot(revs), edits)
    assert (a["edits_90d"], a["edits_30d"], a["reverted_90d"], a["reverted_30d"]) == (4, 3, 2, 1)
    assert a["flagged_30d"] == 2
    assert pa.activity(snapshot(revs))["flagged_30d"] == 0


def test_editors_groups_ip_and_temporary_accounts():
    users = {"Alice": account(400, editcount=50, page_share=0.8)}
    revs = [rev(i, 3, "Alice", reverted=i == 0) for i in range(10)] + [
        rev(20, 3, "1.2.3.4", "anonymous"),
        rev(21, 3, "5.6.7.8", "anonymous"),
        rev(22, 3, "~2026-1", "temporary"),
    ]
    rows = pa.editors(revs, users, AS_OF)
    assert [(r["name"], r["edits"]) for r in rows] == [
        ("Alice", 10),
        (EDITOR_LABELS["anonymous"], 2),
        (EDITOR_LABELS["temporary"], 1),
    ]
    alice = rows[0]
    assert alice == {
        "name": "Alice",
        "kind": "registered",
        "edits": 10,
        "share": 0.769,
        "account_age_days": 400,
        "edit_count": 50,
        "spa": True,
        "reverted": 1,
    }
    assert rows[1]["spa"] is None and rows[1]["account_age_days"] is None


def test_spa_count_matches_the_editors_table_and_skips_temporary_accounts():
    users = {"Alice": account(400, page_share=0.8), "~2026-1": account(5, page_share=1.0)}
    revs = [rev(i, 3, "Alice") for i in range(10)] + [
        rev(20 + i, 3, "~2026-1", "temporary") for i in range(10)
    ]
    assert pa.activity(snapshot(revs, users))["spa_count"] == 1
    assert sum(bool(r["spa"]) for r in pa.editors(revs, users, AS_OF)) == 1


def test_controversy_pattern_skips_ordinary_career_headings():
    assert not pa.CONTROVERSY.search("Legal career")
    assert pa.CONTROVERSY.search("Legal issues")
    assert pa.CONTROVERSY.search("Legal proceedings")
    assert pa.controversy_rank([sec("(lead)", 0, 10), sec("Legal career", 2, 50)]) is None


def test_controversy_rank_and_words_include_subsections():
    sections = [
        sec("(lead)", 0, 100),
        sec("Early life", 2, 50),
        sec("Controversies", 2, 40),
        sec("Expenses claims", 3, 20),
        sec("Personal life", 2, 30),
    ]
    assert pa.controversy_rank(sections) == 2
    assert pa.controversy_words(sections) == 60
    assert pa.controversy_rank(sections[:2]) is None


def test_structure_bloat_index_and_rank_move():
    then = [sec("(lead)", 0, 100), sec("Criticism", 2, 60)]
    now = [sec("(lead)", 0, 200), sec("Career", 2, 100), sec("Criticism", 2, 50)]
    s = pa.structure(snapshot([], then=then, now=now))
    assert (s["words_then"], s["words_now"]) == (160, 350)
    assert (s["controversy_words_then"], s["controversy_words_now"]) == (60, 50)
    assert (s["controversy_share_then"], s["controversy_share_now"]) == (0.375, 0.143)
    assert s["bloat_index"] == 1.25  # (350 - 160 - (50 - 60)) / 160
    assert (s["controversy_rank_then"], s["controversy_rank_now"]) == (1, 2)
    assert pa.rank_move(s) == 1


def test_structure_of_a_page_created_in_the_window():
    s = pa.structure(snapshot([], now=[sec("(lead)", 0, 40), sec("Legal issues", 2, 10)]))
    assert s["words_then"] is None and s["bloat_index"] is None and s["controversy_rank_then"] is None
    assert s["controversy_rank_now"] == 1 and s["controversy_share_now"] == 0.2
    assert pa.rank_move(s) == 0


def test_structure_of_an_empty_article_has_no_shares():
    s = pa.structure(snapshot([], then=[sec("(lead)", 0, 0)], now=[sec("(lead)", 0, 0)]))
    assert s["controversy_share_then"] is None and s["controversy_share_now"] is None
    assert s["bloat_index"] is None


def test_structure_with_the_current_text_hidden_compares_nothing():
    snap = {**snapshot([], then=[sec("(lead)", 0, 40), sec("Criticism", 2, 10)]), "sections_now": None}
    s = pa.structure(snap)
    assert s["controversy_rank_then"] is None and s["controversy_rank_now"] is None
    assert s["words_now"] is None and s["bloat_index"] is None and s["controversy_share_now"] is None
    assert pa.controversy_drop(s) == 0  # not scored as a controversy section that went
    assert pa.analyse(snap)["risk_parts"]["controversy_move"] == 0


def test_risk_score_parts():
    analytics = {
        "flagged_30d": 7,
        "top3_share": 0.5,
        "reverted_30d": 1,
        "new_account_share": 0.25,
        "spa_count": 1,
    }
    struct = {"controversy_rank_then": 1, "controversy_rank_now": 5}
    score, parts = pa.risk(analytics, struct)
    assert parts == {
        "flagged": 30,
        "concentration": 10,
        "controversy_move": 15,
        "reverted": 5,
        "new_accounts": 2.5,
        "spa": 10,
    }
    assert score == 72.5
    up = {"controversy_rank_then": 4, "controversy_rank_now": 1}
    assert pa.risk({**analytics, "spa_count": 0}, up)[1]["controversy_move"] == 0
    gone = {"controversy_rank_then": 4, "controversy_rank_now": None}
    assert pa.risk(analytics, gone)[1]["controversy_move"] == 15
    new = {"controversy_rank_then": None, "controversy_rank_now": 2}
    assert pa.risk(analytics, new)[1]["controversy_move"] == 0


def test_analyse_marks_the_score_partial_until_every_recent_edit_is_classified():
    revs = [rev(1, 60), rev(2, 3), rev(3, 1)]
    snap = snapshot(revs, then=[sec("(lead)", 0, 10)])
    out = pa.analyse(snap, computed_at=AS_OF)
    assert out["risk_partial"] is True
    assert out["structure"]["negative_words_removed"] is None
    assert set(out) == {
        "title",
        "pageid",
        "as_of",
        "days",
        "computed_at",
        "risk_score",
        "risk_partial",
        "risk_parts",
        "analytics",
        "structure",
        "editors",
    }
    edits = {
        2: {
            "classification": {"severity": 3},
            "extract": {"negative_info": "removed"},
            "measured": {"words_removed": 40},
        },
        3: {
            "classification": {"severity": 0},
            "extract": {"negative_info": "none"},
            "measured": {"words_removed": 9},
        },
    }
    full = pa.analyse(snap, edits, computed_at=AS_OF)
    assert full["risk_partial"] is False
    assert full["structure"]["negative_words_removed"] == 40
    assert full["risk_parts"]["flagged"] == 6
