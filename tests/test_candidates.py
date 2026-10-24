import json

from triage import candidates as cd


def analysis(title, risk_score=20.0, reverted=0, bursts=(), struct=None, top3=0.5, edits=10):
    s = {
        "controversy_rank_then": None,
        "controversy_rank_now": None,
        "controversy_share_then": None,
        "controversy_share_now": 0.0,
        "bloat_index": None,
        **(struct or {}),
    }
    return {
        "title": title,
        "pageid": len(title),
        "days": 90,
        "risk_score": risk_score,
        "risk_parts": {},
        "analytics": {
            "reverted_90d": reverted,
            "burst_days": list(bursts),
            "spa_count": 0,
            "new_account_share": 0.0,
            "top3_share": top3,
            "edits_90d": edits,
        },
        "structure": s,
    }


BLOATED = {
    "controversy_rank_then": 1,
    "controversy_rank_now": 4,
    "controversy_share_then": 0.3,
    "controversy_share_now": 0.1,
    "bloat_index": 0.5,
}


def test_extra_parts():
    parts = cd.extra_parts(analysis("A", reverted=10, bursts=["2026-10-01"], struct=BLOATED))
    assert parts == {"bloat": 5, "controversy_drop": 10, "reverted_90d": 10, "bursts": 1.7}
    assert cd.extra_parts(analysis("B")) == {
        "bloat": 0,
        "controversy_drop": 0,
        "reverted_90d": 0,
        "bursts": 0,
    }


def test_growth_only_counts_as_bloat_when_the_controversy_share_falls():
    grown = {**BLOATED, "controversy_share_now": 0.3}
    assert not cd.bloats(analysis("A", struct=grown)["structure"])
    assert cd.bloats(analysis("A", struct=BLOATED)["structure"])
    assert not cd.bloats(analysis("A")["structure"])
    assert not cd.bloats(analysis("A", struct={**BLOATED, "controversy_share_now": None})["structure"])


def test_bloat_needs_a_lot_of_growth_and_the_controversy_section_moving_down():
    assert not cd.bloats(analysis("A", struct={**BLOATED, "bloat_index": 0.01})["structure"])
    assert cd.bloats(analysis("A", struct={**BLOATED, "bloat_index": cd.BLOAT_MIN})["structure"])
    same_place = {**BLOATED, "controversy_rank_now": 1}
    assert not cd.bloats(analysis("A", struct=same_place)["structure"])
    assert cd.bloats(analysis("A", struct={**BLOATED, "controversy_rank_now": None})["structure"])


def test_a_controversy_section_that_disappeared_scores_and_is_named():
    pa = analysis("A", struct={"controversy_rank_then": 3, "controversy_rank_now": None})
    assert cd.extra_parts(pa)["controversy_drop"] == 10
    assert cd.reason(pa).startswith("controversy section gone (was #3)")


def test_reason_names_each_signal_and_ends_with_concentration():
    r = cd.reason(analysis("A", reverted=2, bursts=["2026-10-01", "2026-10-09"], struct=BLOATED, top3=0.8))
    assert r == (
        "controversy section down 3 (#1 -> #4); "
        "grew +50% outside controversy sections, their share 30% -> 10%; "
        "2 reverted in 90d; 2 burst day(s), latest 2026-10-09; "
        "top 3 editors made 80% of 10 edits"
    )
    assert cd.reason(analysis("B")) == "top 3 editors made 50% of 10 edits"


def test_rank_orders_by_score_then_title(tmp_path):
    rows = cd.rank(
        [
            analysis("Quiet", risk_score=10),
            analysis("Busy", risk_score=10, reverted=5),
            analysis("Also quiet", risk_score=10),
            analysis("Shifted", risk_score=15, struct=BLOATED),
        ]
    )
    assert [(r["rank"], r["title"], r["score"]) for r in rows] == [
        (1, "Shifted", 30),  # 15 + bloat 5 + controversy section down 3 places 10
        (2, "Busy", 20),
        (3, "Also quiet", 10),
        (4, "Quiet", 10),
    ]
    path = cd.write(rows, "2026-10-24T12:00:00+00:00", 90, tmp_path / "candidates.json")
    doc = json.loads(path.read_text())
    assert doc["candidates"] == rows and doc["weights"] == cd.EXTRA_WEIGHTS
