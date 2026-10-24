import pytest

from triage.intent import (
    DECLARED,
    DECLARED_TO_WIKI,
    REVERT_TOOLS,
    REVIEW_REASONS,
    REVIEW_TO_WIKI,
    declared_intent,
    revert_tool,
    review_reason,
)
from triage.prompt import WIKI_REASONS

TALK = "[[Special:Contributions/{u}|{u}]] ([[User talk:{u}|talk]])"


def by(user="Example"):
    return TALK.format(u=user)


# Revert summaries as patrol tools and patrollers write them (from the 2026-10 corpus).
REVIEWS = [
    (f"Reverted 2 edits by {by()}: Addition of unsourced content", "unsourced"),
    (f"Reverted [[WP:AGF|good faith]] edits by {by()}", "good_faith"),
    (f"Reverted [[WP:AGF|good faith]] edits by {by()}: Rv - keeping order chronological", "good_faith"),
    (
        f"Reverted edits by {by()}: Unexplained deletion of content ([[:en:WP:WikiShield|WS]])",
        "unexplained_removal",
    ),
    (f"Reverted edits by {by()}: Non-English content ([[:en:WP:WikiShield|WS]])", "non_english"),
    (f"Reverted edits by {by()}: vandalism (VandalHandle)", "vandalism"),
    (
        f"Reverting possible vandalism by {by('1.2.3.4')} to version by Y. [[WP:CBFP|Report False Positive?]]"
        " Thanks, [[WP:CBNG|ClueBot NG]]. (4412345) (Bot)",
        "vandalism",
    ),
    ("alibaba spammer; LLM content and promotional links", "promotional"),
    (
        f"Undid revision [[Special:Diff/1378334643|1378334643]] by {by('~2026-53101-60')} use of AI",
        "ai_generated",
    ),
    (
        f"Undid revision [[Special:Diff/1377196150|1377196150]] by {by()} blogs are not acceptable sources",
        "unsourced",
    ),
    (
        f"Reverted edits by {by()}: adding [[WP:ELNO|inappropriate links]] ([[:en:WP:WikiShield|WS]])",
        "promotional",
    ),
    ("[[WP:BLOCKREVERT]]", "ban_evasion"),
    ("rvv", "vandalism"),
    (f"Undid revision [[Special:Diff/1|1]] by {by()} wrong page", "other_stated"),
    (f"Undid revision [[Special:Diff/1381001824|1381001824]] by {by('İmmortalance')}", "none"),
    (f"Reverted 1 edit by {by('~2026-55548-44')} to last revision by BroadwayEditor", "none"),
    (
        "Reverted edits by [[Special:Contribs/Kent dems|Kent dems]] ([[User talk:Kent dems|talk]])"
        " to last version by CopyleftEverything",
        "none",
    ),
    (f"Reverted edits by {by()} ([[WP:HG|HG]]) (3.4.14)", "none"),
    (f"[[WP:CVPI|Interceptor]]: Reverting non-constructive edits by {by()}", "none"),
    ("RV Undid 2 revisions from [[Special:Diff/1|1]] until [[Special:Diff/2|2]]", "none"),
    ("/* Schedule */", "none"),
    ("", "none"),
]


@pytest.mark.parametrize(("comment", "reason"), REVIEWS)
def test_review_reason_on_real_summaries(comment, reason):
    assert review_reason(comment) == reason


@pytest.mark.parametrize(
    ("comment", "tags", "user", "tool"),
    [
        (f"Reverted 2 edits by {by()}: unsourced", ["twinkle", "mw-undo"], "P", "twinkle"),
        (f"Reverted edits by {by()} ([[WP:HG|HG]]) (3.4.14)", ["huggle", "mw-rollback"], "P", "huggle"),
        ("Reverting possible vandalism by X", [], "ClueBot NG", "cluebot"),
        (f"Reverted edits by {by()} ([[:en:WP:WikiShield|WS]])", ["WikiShield script"], "P", "wikishield"),
        (
            f"[[WP:CVPI|Interceptor]]: Reverting non-constructive edits by {by()}",
            ["CVPI"],
            "P",
            "interceptor",
        ),
        ("rv", ["RedWarn"], "P", "other_tool"),
        (f"Reverted edits by {by()} to last version by Y", ["mw-rollback"], "P", "rollback"),
        (f"Undid revision 1 by {by()}", ["mw-undo"], "P", "undo"),
        ("restore earlier version", ["mw-manual-revert"], "P", "manual"),
    ],
)
def test_revert_tool(comment, tags, user, tool):
    assert revert_tool(comment, tags, user) == tool


@pytest.mark.parametrize(
    ("comment", "intent"),
    [
        ("ce", "copyedit"),
        ("/* Plot */ typo", "copyedit"),
        ("fixed grammar and punctuation", "copyedit"),
        ("/* History */ added ref", "references"),
        ("Rescuing 2 sources and tagging 0 as dead.) #IABot (v2.0.9.5", "references"),
        ("removed unsourced claim", "removal"),
        ("rm promo", "removal"),
        ("update stats", "update"),
        ("/* Results */ 2026 results", "update"),
        ("wikilink", "formatting"),
        ("infobox", "formatting"),
        ("added info about his second album", "content"),
        ("expanded section", "content"),
        ("/* See also */ +[[Revels (confectionery)|Revels]]", "content"),
        ("Aggiunta media dei sondaggi", "other"),
        ("/* Early life */", "none"),
        ("", "none"),
        (None, "none"),
        ("Replaced content with 'hello'", "none"),
        ("←Created page with 'x'", "none"),
    ],
)
def test_declared_intent(comment, intent):
    assert declared_intent(comment) == intent


def test_every_class_maps_onto_the_sonnet_reasons():
    assert set(REVIEW_TO_WIKI) == set(REVIEW_REASONS)
    assert set(DECLARED_TO_WIKI) == set(DECLARED)
    assert set(REVIEW_TO_WIKI.values()) <= set(WIKI_REASONS)
    assert set(DECLARED_TO_WIKI.values()) <= set(WIKI_REASONS)
    assert len(set(REVERT_TOOLS)) == len(REVERT_TOOLS)
