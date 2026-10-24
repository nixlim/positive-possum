import pytest

from triage import pipeline

API_REVS = [
    {
        "revid": 101,
        "parentid": 100,
        "timestamp": "2026-10-23T01:00:00Z",
        "user": "Zed",
        "userid": 4,
        "comment": "c",
        "size": 80,
        "tags": [],
        "minor": True,
    },
    {
        "revid": 102,
        "parentid": 0,
        "timestamp": "2026-10-23T02:00:00Z",
        "user": "5.6.7.8",
        "anon": True,
        "size": 10,
        "tags": [],
    },
]
API_USER = {"name": "Zed", "editcount": 3, "registration": "2026-01-01T00:00:00Z", "groups": ["bot"]}


class FakeSession:
    def __init__(self):
        self.queries = []

    def query(self, params):
        self.queries.append(params)
        if params.get("list") == "users":
            return {"query": {"users": [API_USER, {"name": "Gone", "missing": True}]}}
        if params["rvprop"] == "ids|size":
            parents = [{"revid": 100, "size": 50}]
            return {"query": {"pages": [{"pageid": 7, "ns": 0, "title": "Acme", "revisions": parents}]}}
        talk = {"pageid": 9, "ns": 1, "title": "Talk:Acme", "revisions": [{"revid": 103, "parentid": 1}]}
        return {"query": {"pages": [{"pageid": 7, "ns": 0, "title": "Acme", "revisions": API_REVS}, talk]}}


def test_fetch_revisions_builds_snapshot_dicts():
    s = FakeSession()
    revs = pipeline.fetch_revisions(s, [101, 102, 103])
    assert [q.get("revids") for q in s.queries] == ["101|102|103", "100", None]
    users_q = s.queries[2]
    assert (users_q["ususers"], users_q["usprop"]) == ("Zed", "editcount|registration|groups")
    assert [r["revid"] for r in revs] == [101, 102]  # mainspace only
    a, b = revs
    assert (a["size_delta"], a["minor"], a["editor_kind"]) == (30, True, "registered")
    assert a["account"] == {"editcount": 3, "registration": "2026-01-01T00:00:00Z", "groups": ["bot"]}
    assert pipeline.is_bot(a)  # groups fetched, so the run drops it
    assert (b["size_delta"], b["editor_kind"], b["account"], b["comment"]) == (10, "anonymous", None, "")


def test_fetch_revisions_refuses_more_than_one_batch():
    with pytest.raises(SystemExit):
        pipeline.fetch_revisions(FakeSession(), list(range(51)))
