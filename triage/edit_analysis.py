"""Tables of the edit corpus (pandas) for notebooks/edit_intent.ipynb: the population of
edits, how patrollers revert, the stated reasons, and the feature table the models train on."""

from __future__ import annotations

import numpy as np
import pandas as pd

from .corpus import is_human_content
from .features import SAVE_TIME_TAGS, edit_features, summary_text
from .intent import DECLARED_TO_WIKI, REVIEW_TO_WIKI, declared_intent, revert_tool, review_reason
from .wiki import REVERTED_TAG, editor_kind

DEVICE_TAGS = (
    ("mobile app edit", "mobile app"),
    ("mobile web edit", "mobile web"),
    ("mobile edit", "mobile"),
)
INTERFACE_TAGS = (("visualeditor", "VisualEditor"), ("wikieditor", "source editor (2010)"))
EDITCOUNT_BINS = [-1, 0, 9, 99, 999, 9_999, np.inf]
EDITCOUNT_LABELS = ["0", "1-9", "10-99", "100-999", "1k-9.9k", "10k+"]
SIZE_BINS = [-np.inf, -1000, -100, -1, 0, 100, 1000, np.inf]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
SIZE_LABELS = ["< -1k", "-1k..-101", "-100..-1", "0", "1..100", "101..1k", "> 1k"]


def _first(tags: list[str], table: tuple, default: str) -> str:
    return next((name for tag, name in table if tag in tags), default)


def edits_frame(c: dict) -> pd.DataFrame:
    """One row per listed main-namespace edit: the sampled windows and the live-desk edits."""
    rows = []
    for rc in c["edits"]:
        tags = rc.get("tags") or []
        rows.append(
            {
                "revid": rc["revid"],
                "pageid": rc["pageid"],
                "window": rc["window"],
                "split": rc["split"],
                "ts": pd.Timestamp(rc["timestamp"]),
                "user": rc.get("user") or "",
                "kind": "bot" if rc.get("bot") else editor_kind(rc),
                "page_creation": rc["type"] == "new",
                "human_content": is_human_content(rc),
                "reverted": REVERTED_TAG in tags,
                "size_delta": (rc.get("newlen") or 0) - (rc.get("oldlen") or 0),
                "summary_present": bool(summary_text(rc.get("comment"))),
                "device": _first(tags, DEVICE_TAGS, "desktop"),
                "interface": _first(tags, INTERFACE_TAGS, "other / none"),
                "tags": tags,
                "comment": rc.get("comment") or "",
            }
        )
    df = pd.DataFrame(rows)
    df["hour"] = df["ts"].dt.hour
    df["weekday"] = df["ts"].dt.day_name().str[:3]
    df["size_bucket"] = pd.cut(df["size_delta"], SIZE_BINS, labels=SIZE_LABELS)
    return df


def rate_table(df: pd.DataFrame, by: str, weight: str | None = None) -> pd.DataFrame:
    """Edits, share of edits, reverts, share of reverts and revert rate per group of `by`,
    optionally weighted (the diff sample is case-control)."""
    w = df[weight] if weight else pd.Series(1.0, index=df.index)
    g = pd.DataFrame({"g": df[by], "w": w, "r": w * df["reverted"]}).groupby("g", observed=True)
    out = pd.DataFrame({"edits": g["w"].sum(), "reverts": g["r"].sum()})
    out["share_of_edits"] = out["edits"] / out["edits"].sum()
    out["share_of_reverts"] = out["reverts"] / out["reverts"].sum()
    out["revert_rate"] = out["reverts"] / out["edits"]
    out.index.name = by
    return out.round({"edits": 0, "reverts": 0})


def volume(edits: pd.DataFrame, by: str) -> pd.DataFrame:
    """Mean main-namespace edits per sampled hour, grouped by `by`, human against bot."""
    w = edits[edits["window"] >= 0]
    who = w["kind"].eq("bot").map({True: "bot", False: "human"}).rename("who")
    per_window = pd.crosstab([w[by], w["window"]], who)
    out = per_window.groupby(level=0).mean()[["human", "bot"]]
    return out.reindex(WEEKDAYS) if by == "weekday" else out


def editcount_table(sample: pd.DataFrame) -> pd.DataFrame:
    """Revert rate by the editor's edit count, from the diff sample (weighted to the natural
    rate). The count is read when the corpus is built, so it includes later edits."""
    s = sample[sample["split"] != "live"]
    count = np.expm1(s["log_editcount"])
    bucket = pd.cut(count, EDITCOUNT_BINS, labels=EDITCOUNT_LABELS).astype(object)
    s = s.assign(editcount=bucket.where(count.notna(), "IP (no account)"))
    table = rate_table(s, "editcount", "weight").reindex(["IP (no account)", *EDITCOUNT_LABELS])
    return table.dropna(subset=["edits"])


def reasons_by_kind(links: pd.DataFrame, order, min_reverts: int = 30) -> pd.DataFrame:
    """Row shares of each stated reason per editor kind, for kinds with enough reverts."""
    kinds = links["kind"].value_counts()
    rows = links[links["kind"].isin(kinds[kinds >= min_reverts].index)]
    return pd.crosstab(rows["kind"], rows["reason"], normalize="index").reindex(columns=order, fill_value=0)


def declared_shares(edits: pd.DataFrame) -> pd.DataFrame:
    """The editor's declared intent (from their own summary), kept against reverted, as a
    share of human content edits in the sampled windows."""
    w = edits[(edits["window"] >= 0) & edits["human_content"]]
    declared = w["comment"].map(declared_intent)
    out = pd.crosstab(declared, w["reverted"].map({False: "kept", True: "reverted"}), normalize="columns")
    return out.sort_values("kept", ascending=False)


def tag_rates(df: pd.DataFrame, tags=SAVE_TIME_TAGS, min_edits: int = 30) -> pd.DataFrame:
    """Revert rate of edits carrying each save-time tag, against the rate without it."""
    rows = []
    for t in tags:
        has = df["tags"].map(lambda ts, t=t: t in ts)
        if has.sum() >= min_edits:
            rows.append(
                {
                    "tag": t,
                    "edits": int(has.sum()),
                    "revert_rate": df.loc[has, "reverted"].mean(),
                    "rate_without": df.loc[~has, "reverted"].mean(),
                }
            )
    return pd.DataFrame(rows).set_index("tag").sort_values("revert_rate", ascending=False)


def links_frame(c: dict, edits: pd.DataFrame) -> pd.DataFrame:
    """One row per reverted edit linked to its revert: delay, tool, reverter, stated reason."""
    links = list(c["links"].values())
    df = pd.DataFrame(links)
    df["delay_min"] = df["delay_s"] / 60
    df["tool"] = [revert_tool(x["reverter_comment"], x["reverter_tags"], x["reverter_user"]) for x in links]
    df["reason"] = df["reverter_comment"].map(review_reason)
    df["reason_wiki"] = df["reason"].map(REVIEW_TO_WIKI)
    df["stated"] = df["reason"] != "none"
    # ClueBot NG edits without the bot flag (so patrollers see its reverts); count it as a bot.
    automated = df["reverter_bot"].fillna(False).astype(bool) | (df["tool"] == "cluebot")
    df["reverter_kind"] = np.where(automated, "bot", "human")
    cols = ["revid", "kind", "window", "split", "human_content", "ts"]
    return df.merge(edits[cols], on="revid", how="left")


def delay_summary(links: pd.DataFrame) -> pd.Series:
    d = links["delay_min"]
    return pd.Series(
        {
            "reverts": len(d),
            "median_min": d.median(),
            "p90_min": d.quantile(0.9),
            "within_10_min": (d <= 10).mean(),
            "within_1_h": (d <= 60).mean(),
            "within_3_h": (d <= 180).mean(),
            "within_24_h": (d <= 1440).mean(),
        }
    )


# ---- the model table --------------------------------------------------------------------


def sample_frame(c: dict) -> pd.DataFrame:
    """The diff sample and the live-desk edits with features, added text and both labels.
    `weight` restores the natural revert rate (1 / sampling fraction, live edits 1)."""
    users, diffs, links = c["users"], c["diffs"], c["links"]
    rows = []
    for rc in c["edits"]:
        diff = diffs.get(rc["revid"])
        if diff is None or not is_human_content(rc):
            continue
        link = links.get(rc["revid"]) or {}
        reverted = REVERTED_TAG in (rc.get("tags") or [])
        reason = review_reason(link["reverter_comment"]) if link else None
        declared = declared_intent(rc.get("comment"))
        rows.append(
            {
                "revid": rc["revid"],
                "window": rc["window"],
                "split": rc["split"],
                "kind": editor_kind(rc),
                "reverted": reverted,
                "review_reason": reason,
                "declared": declared,
                "wiki_reason": REVIEW_TO_WIKI[reason] if reason else DECLARED_TO_WIKI[declared],
                "added": diff.get("added") or "",
                "removed": diff.get("removed") or "",
                **edit_features(rc, diff, users.get(rc.get("user") or "")),
            }
        )
    df = pd.DataFrame(rows)
    pop = c["manifest"]["sampling"]["population"]
    corpus = df["split"] != "live"
    n_rev = (corpus & df["reverted"]).sum()
    n_keep = (corpus & ~df["reverted"]).sum()
    df["weight"] = np.where(df["reverted"], pop["revert"] / n_rev, pop["keep"] / n_keep)
    df.loc[~corpus, "weight"] = 1.0
    return df


def feature_columns(df: pd.DataFrame, with_summary: bool = True) -> list[str]:
    """The numeric model inputs (see features.edit_features)."""
    skip = {"revid", "window", "split", "kind", "reverted", "review_reason", "declared"}
    skip |= {"wiki_reason", "added", "removed", "weight"}
    if not with_summary:
        skip |= {"summary_present", "summary_section_only", "summary_len"}
    return [c for c in df.columns if c not in skip]
