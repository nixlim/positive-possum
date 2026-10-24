"""Two noisy views of an edit's intention, read from edit summaries. Pure functions.

- The reviewer's stated reason: why the edit was reverted, from the reverting edit's summary
  (Twinkle's "Reverted 1 edit by X: Addition of unsourced content", ClueBot NG's "Reverting
  possible vandalism", free text). Many reverts state none.
- The editor's declared intent: what the editor said the edit does, from its own summary
  ("ce", "added ref", "updated stats").

Neither is the true intent: one is a patroller's label, the other a self-description. Both
map onto `prompt.WIKI_REASONS`, the reason set the live desk asks Sonnet for."""

from __future__ import annotations

import re

from .features import summary_text

# ---- the reviewer's stated reason (from the revert summary) ---------------------------

REVIEW_REASONS = (
    "vandalism",
    "unsourced",
    "unexplained_removal",
    "promotional",
    "test",
    "non_english",
    "pov",
    "copyright",
    "ai_generated",
    "ban_evasion",
    "good_faith",
    "other_stated",
    "none",
)
REVIEW_TO_WIKI = {
    "vandalism": "vandalism",
    "unsourced": "unsourced",
    "unexplained_removal": "other",
    "promotional": "promotional",
    "test": "test_edit",
    "non_english": "other",
    "pov": "pov",
    "copyright": "other",
    "ai_generated": "other",
    "ban_evasion": "other",
    "good_faith": "other",
    "other_stated": "other",
    "none": "other",
}

# Tool and template boilerplate around the reason, matched after wiki links are reduced to
# their text. Removed before reading the reason, so "Reverted 2 edits by X (talk) to last
# revision by Y (HG) [3.4.13]" leaves nothing. A user name runs to "(talk)", ":", "(", " to "
# or the end.
_NAME = r"[^:(]+?(\s*\(talk\))?(?=\s+to\s|\s*[:(]|$)"
_BOILERPLATE = [
    rf"(reverted|reverting|rollback( of)?|undid|undoing|undo) (\d+ )?(pending )?(good faith |non.?constructive |unconstructive )?"
    rf"(edits?|edit\(s\)) by {_NAME}",
    rf"(undid|undo|undoing) revision \d+ by {_NAME}",
    r"(undid|undo|undoing) revision \d+",
    r"(undid|undo|undoing) \d+ revisions? from \d+ until \d+",
    rf"restored revision \d+ by {_NAME}",
    rf"to (the )?(last |previous )?(good )?(version|revision|rev\.?)( \d+)?( by {_NAME})?",
    r"^\s*(rvt?|revert(ed|ing)?|undo|undid|restored?)\b",  # not "rvv", which means vandalism
    # Interceptor's default and many patrollers' generic flag: says "revert", not why.
    r"\b(reverting |reverted )?(non.?constructive|unconstructive)( edits?)?",
    r"report false positive\?.*",
    r"\((talk|bot)\)",
    r"\(?\b(hg|tw|uv|rw|av|ws|wikishield|ultraviolet|redwarn|swviewer|antivandal|vandalhandle|"
    r"interceptor|wp:cvpi)\b\)?",
    r"\[\d+(\.\d+)+\]|\bv?\d+(\.\d+)+\b|\(\d+\)",  # version numbers, ClueBot's (12345)
]
_BOILER = [re.compile(p, re.I) for p in _BOILERPLATE]
_LINK = re.compile(r"\[\[(?:[^\]|]*\|)?([^\]]*)\]\]")

# Most specific first: the first class whose pattern matches wins.
_REASON_RX = [
    ("copyright", r"copyright|copyvio|copy.?paste|plagiari|close paraphras|copied (from|text)|\bcv\b"),
    (
        "vandalism",
        r"vandal|\brvv\b|nonsense|gibberish|hoax|\bjoke|silly|troll|disruptive|sneaky|"
        r"misinformation|false information|deliberate(ly)? (false|factual)",
    ),
    (
        "promotional",
        r"promot|promo\b|spam|advert|\bads?\b|\bcoi\b|conflict of interest|external link|"
        r"\bwp:el|\belno\b|inappropriate links?|linkspam|linkfarm|commercial|marketing|peacock|puff|"
        r"self.?publish",
    ),
    (
        "non_english",
        r"non.?english|not (in )?english|english only|foreign language|wrong language|"
        r"machine.?translat|untranslated",
    ),
    ("test", r"\btest(ing)?\b|sandbox|experiment"),
    (
        "pov",
        r"\bn?pov\b|neutral|opinion|bias|\bblp|editoriali|weasel|undue|soapbox|fringe|"
        r"pejorative|subjective|biograph(y|ies) of living",
    ),
    (
        "unexplained_removal",
        r"unexplained (removal|deletion|blanking|content removal|change)|"
        r"removal of (sourced |cited |referenced )?content|blank(ing|ed)|without (an )?explanation|"
        r"\bremov\w* (sourced|cited|referenced)|deletion of (sourced|cited|referenced)",
    ),
    (
        "unsourced",
        r"unsourced|unreferenced|unverif|no (reliable )?(source|ref|citation)|"
        r"without (a |any )?(reliable )?(source|ref|citation)|not (supported|verified|in (the )?source)|"
        r"citation needed|\bwp:v\b|verif|original research|\bwp:or\b|unreliable|\bwp:rs\b|reliable source|"
        r"speculat|crystal|rumou?r|needs? (a )?(source|ref|citation)|sourc|\bcite\b|\bblogs?\b",
    ),
    ("ban_evasion", r"\bsock|blockrevert|block.?evasion|evading|\blta\b|\bbanned\b|wp:ban|wp:bmb|wp:deny"),
    ("good_faith", r"good.?faith|\bagf\b|\bgf\b"),
]
_REASON = [(name, re.compile(rx, re.I)) for name, rx in _REASON_RX]
# AI-written text, after spam (a spammer's LLM text is spam) and before the rest. "AI" and
# "LLM" are matched case-sensitively; the phrases in any case.
_REASON.insert(
    3,
    (
        "ai_generated",
        re.compile(r"\b(AI|LLMs?)\b|(?i:chat.?gpt|ai.generated|machine.generated|wp:llm|wp:ai\w*)"),
    ),
)
_ORIGINAL_RESEARCH = re.compile(r"\bOR\b")  # case-sensitive: "or" is a common word


def _strip_boilerplate(comment: str) -> str:
    text = summary_text(_LINK.sub(r"\1", comment or ""))
    for rx in _BOILER:
        text = rx.sub(" ", text)
    return re.sub(r"[\s:;,.\-–|()]+", " ", text).strip()


def review_reason(comment: str | None) -> str:
    """The reviewer's stated reason for a revert, from the reverting edit's summary."""
    comment = comment or ""
    if re.search(r"reverting possible vandalism|identified as vandalism", comment, re.I):
        return "vandalism"
    rest = _strip_boilerplate(comment)
    for name, rx in _REASON:
        if rx.search(rest):
            return name
    if _ORIGINAL_RESEARCH.search(rest):
        return "unsourced"
    if re.search(r"reverted good faith edits", _LINK.sub(r"\1", comment), re.I):
        return "good_faith"
    return "other_stated" if len(re.findall(r"[a-z]", rest, re.I)) >= 3 else "none"


# ---- the reverting tool -------------------------------------------------------------

REVERT_TOOLS = (
    "cluebot",
    "huggle",
    "twinkle",
    "wikishield",
    "interceptor",
    "other_tool",
    "rollback",
    "undo",
    "manual",
)
_TOOL_RX = [
    ("cluebot", r"cluebot"),
    ("huggle", r"\bhuggle\b|wp:hg\b|\(hg\)"),
    ("twinkle", r"\btwinkle\b|wp:tw\b|\(tw\)"),
    ("wikishield", r"wikishield"),
    ("interceptor", r"\bcvpi\b|interceptor"),
    ("other_tool", r"ultraviolet|redwarn|\bwp:rw\b|swviewer|antivandal|vandalhandle|\bstiki\b"),
]
_TOOL = [(name, re.compile(rx, re.I)) for name, rx in _TOOL_RX]


def revert_tool(comment: str | None, tags: list[str], user: str | None = None) -> str:
    """Which tool made a revert: a named patrol tool (by its tag, summary signature or bot
    name), else MediaWiki's rollback, undo, or a manual revert."""
    hay = " ".join([comment or "", *tags, user or ""])
    for name, rx in _TOOL:
        if rx.search(hay):
            return name
    if "mw-rollback" in tags:
        return "rollback"
    if "mw-undo" in tags:
        return "undo"
    return "manual"


# ---- the editor's declared intent (from the edit's own summary) -------------------------

DECLARED = ("references", "removal", "copyedit", "update", "formatting", "content", "other", "none")
DECLARED_TO_WIKI = {
    "references": "constructive",
    "removal": "constructive",
    "copyedit": "copyedit",
    "update": "constructive",
    "formatting": "copyedit",
    "content": "constructive",
    "other": "constructive",
    "none": "constructive",
}
# Summaries MediaWiki writes itself: the editor declared nothing.
_AUTOMATIC = re.compile(
    r"^(replaced content with|blanked the page|removed redirect to|redirected page to|"
    r"changed redirect target|←?\s*created page with|undid revision)",
    re.I,
)
_DECLARED_RX = [
    (
        "references",
        r"\brefs?\b|reflist|reference|\bcit(e|ed|es|ation|ations)\b|\bsourc|\bdoi\b|isbn|\burls?\b|"
        r"iabot|rescuing \d+ sources|dead ?link|archive|\bwp:ucb\b|\bbare url",
    ),
    ("removal", r"\bremov|\brm\b|\brmv\b|\btrim|\bdelet|\bcut\b|\bcull|\bprun"),
    (
        "copyedit",
        r"\bc/?e\b|copy.?edit|typo|grammar|spelling|\bsp\b|punctuat|capitali|wording|"
        r"rephras|reword|phrasing|\bfix(ed|es|ing)?\b|\bcorrect|clarif|tense|\bmisspel|tweak|clean.?up|"
        r"simplif|\b(sentence|lower|upper) ?case\b|standardi[sz]|\btone\b|\bminor\b|read(s)? better|\bflow\b",
    ),
    (
        "update",
        r"updat|\bstats?\b|statistics|results?\b|score|standings|\bcareer\b|season|"
        r"\bupd\b|current|latest|\bas of\b|ranking|population|census",
    ),
    (
        "formatting",
        r"wikilink|\blink(s|ed|ing)?\b|template|infobox|categor|\bcats?\b|image|photo|coord|"
        r"defaultsort|retarget|redirect|"
        r"\bfile:|\bmos\b|\bwp:mos|format|layout|short description|\bsd\b|disambig|\bdab\b|"
        r"\btag(s|ged)?\b|maintenance|heading|table|style|italic|bold|order|\bsort|caption|"
        r"\bnav(box)?\b|convert|\bdate format|\bspacing|whitespace|\bnbsp\b",
    ),
    ("content", r"\badd|\bexpan|\binfo\b|information|\bnew\b|wrote|written|\bmore\b|\bdetail|^\+|\s\+"),
]
_DECLARED = [(name, re.compile(rx, re.I)) for name, rx in _DECLARED_RX]


def declared_intent(comment: str | None) -> str:
    """What the editor said the edit does. The automatic `/* Section */` prefix and
    MediaWiki's own summaries count as nothing declared."""
    typed = summary_text(comment)
    if not typed or _AUTOMATIC.match(typed):
        return "none"
    for name, rx in _DECLARED:
        if rx.search(typed):
            return name
    return "other"
