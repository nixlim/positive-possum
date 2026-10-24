"""Matplotlib charts for notebooks/edit_intent.ipynb, in one visual system: a light surface,
recessive hairline grid, thin marks, categorical hues in a fixed order (never cycled), one
blue ramp for magnitude, text in ink colours only. Each chart sits beside its table."""

from __future__ import annotations

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from matplotlib.colors import LinearSegmentedColormap

SURFACE = "#fcfcfb"
INK, INK_2, MUTED = "#0b0b0b", "#52514e", "#898781"
GRID, BASELINE = "#e1e0d9", "#c3c2b7"
SERIES = ("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948")
BLUES = ("#f4f8fe", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#184f95", "#0d366b")
RAMP = LinearSegmentedColormap.from_list("blues", BLUES)
BAR = 0.6  # thin bars: 60% of the slot
PCT = plt.FuncFormatter(lambda v, _: f"{v:.0%}")


def style() -> None:
    plt.rcParams.update(
        {
            "figure.facecolor": SURFACE,
            "axes.facecolor": SURFACE,
            "savefig.facecolor": SURFACE,
            "figure.dpi": 110,
            "font.size": 9,
            "axes.titlesize": 10,
            "axes.titleweight": "bold",
            "axes.titlelocation": "left",
            "axes.titlecolor": INK,
            "axes.labelcolor": INK_2,
            "axes.edgecolor": BASELINE,
            "xtick.color": MUTED,
            "ytick.color": MUTED,
            "xtick.labelcolor": INK_2,
            "ytick.labelcolor": INK_2,
            "legend.frameon": False,
            "legend.labelcolor": INK_2,
            "axes.prop_cycle": plt.cycler(color=SERIES),
        }
    )


def _frame(ax, title: str, grid: str = "x", xlabel: str = "", ylabel: str = "") -> None:
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    for side in ("top", "right", "left" if grid == "x" else "bottom"):
        ax.spines[side].set_visible(False)
    ax.grid(axis=grid, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    ax.tick_params(length=0)


def _fmt(v: float, fmt: str) -> str:
    return fmt.format(v) if np.isfinite(v) else "-"


def hbars(ax, s: pd.Series, title: str, fmt: str = "{:.0%}", xlabel: str = "", color: str = SERIES[0]):
    """One series as thin horizontal bars, largest at the top, value at each bar's end."""
    s = s.iloc[::-1]
    y = np.arange(len(s))
    ax.barh(y, s.values, height=BAR, color=color, edgecolor=SURFACE, linewidth=1)
    ax.set_yticks(y, [str(i) for i in s.index])
    top = np.nanmax(s.values) if len(s) else 1
    for yi, v in zip(y, s.values, strict=True):
        ax.text(v + top * 0.01, yi, _fmt(v, fmt), va="center", color=INK_2, fontsize=8)
    ax.set_xlim(0, top * 1.15)
    _frame(ax, title, "x", xlabel)
    if "%" in fmt:
        ax.xaxis.set_major_formatter(PCT)


def grouped_hbars(ax, df: pd.DataFrame, title: str, fmt: str = "{:.0%}", xlabel: str = ""):
    """Two to four series per row (columns of `df`), in fixed hue order, with a legend."""
    k = len(df.columns)
    h = BAR / k
    y = np.arange(len(df))[::-1]
    for i, col in enumerate(df.columns):
        pos = y + BAR / 2 - h * (i + 0.5)
        ax.barh(pos, df[col].values, height=h, color=SERIES[i], edgecolor=SURFACE, linewidth=1, label=col)
        for p, v in zip(pos, df[col].values, strict=True):
            ax.text(v, p, " " + _fmt(v, fmt), va="center", color=INK_2, fontsize=7)
    ax.set_yticks(y, [str(i) for i in df.index])
    ax.set_xlim(0, np.nanmax(df.values) * 1.2)
    ax.legend(loc="upper left", bbox_to_anchor=(1.0, 1.0))  # outside the plot, never on a bar
    _frame(ax, title, "x", xlabel)
    if "%" in fmt:
        ax.xaxis.set_major_formatter(PCT)


def stacked_columns(ax, df: pd.DataFrame, title: str, ylabel: str = ""):
    """Columns of `df` stacked per index value, surface-coloured gaps between segments."""
    bottom = np.zeros(len(df))
    x = np.arange(len(df))
    for i, col in enumerate(df.columns):
        ax.bar(
            x, df[col].values, BAR, bottom=bottom, color=SERIES[i], edgecolor=SURFACE, linewidth=1, label=col
        )
        bottom += df[col].values
    ax.set_xticks(x, [str(i) for i in df.index])
    ax.legend(loc="upper left", ncols=len(df.columns))
    _frame(ax, title, "y", ylabel=ylabel)


def ecdf(ax, values, title: str, xlabel: str, marks=(10, 60, 180, 1440)):
    """Cumulative share on a log axis, with the share reached at a few reference points."""
    v = np.sort(np.asarray(values, dtype=float))
    v = v[np.isfinite(v)]
    share = np.arange(1, len(v) + 1) / len(v)
    ax.step(np.maximum(v, 0.1), share, where="post", color=SERIES[0], linewidth=2)
    ax.set_xscale("log")
    for m in marks:
        s = (v <= m).mean()
        ax.plot([m], [s], "o", color=SERIES[0], markersize=8, markeredgecolor=SURFACE, markeredgewidth=2)
        ax.annotate(
            f"{s:.0%} by {m:g}", (m, s), (6, -10), textcoords="offset points", color=INK_2, fontsize=8
        )
    ax.set_ylim(0, 1.02)
    ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda y, _: f"{y:.0%}"))
    _frame(ax, title, "y", xlabel)


def heatmap(ax, df: pd.DataFrame, title: str, fmt: str = "{:.0%}", xlabel: str = "", ylabel: str = ""):
    """Magnitude on one blue ramp, the value printed in each cell in ink for contrast."""
    vals = df.to_numpy(dtype=float)
    top = np.nanmax(vals) if np.isfinite(vals).any() else 1
    ax.imshow(vals, cmap=RAMP, vmin=0, vmax=top or 1, aspect="auto")
    ax.set_xticks(range(df.shape[1]), [str(c) for c in df.columns], rotation=35, ha="right")
    ax.set_yticks(range(df.shape[0]), [str(i) for i in df.index])
    for (r, c), v in np.ndenumerate(vals):
        dark = top and v / top > 0.55
        ax.text(c, r, _fmt(v, fmt), ha="center", va="center", fontsize=7, color=SURFACE if dark else INK)
    ax.set_title(title)
    ax.set_xlabel(xlabel)
    ax.set_ylabel(ylabel)
    ax.tick_params(length=0)
    for side in ax.spines.values():
        side.set_visible(False)


def confusion(ax, matrix, labels, title: str):
    """A confusion matrix normalised by row: each true class's spread over predictions."""
    m = np.asarray(matrix, dtype=float)
    rows = m / np.maximum(m.sum(1, keepdims=True), 1)
    heatmap(ax, pd.DataFrame(rows, index=labels, columns=labels), title, xlabel="predicted", ylabel="true")


def pr_curves(ax, curves: dict, base_rate: float, title: str):
    """Precision against recall per model (fixed hue order), the base rate as a reference."""
    for i, (name, (prec, rec)) in enumerate(curves.items()):
        ax.plot(rec, prec, color=SERIES[i], linewidth=2, label=name, drawstyle="steps-post")
    ax.axhline(base_rate, color=MUTED, linewidth=1)
    ax.annotate(
        f"base rate {base_rate:.1%}",
        (0, base_rate),
        (4, 4),
        textcoords="offset points",
        ha="left",
        color=INK_2,
        fontsize=8,
    )
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1.02)
    ax.legend(loc="upper right")
    _frame(ax, title, "y", "recall", "precision")


def calibration_plot(ax, cals: dict[str, dict], title: str):
    """Mean predicted probability against the observed revert rate per score bin, one line
    per model (hues in the same order as pr_curves)."""
    top = max(max(np.max(c["predicted"]), np.max(c["observed"])) for c in cals.values()) * 1.05
    ax.plot([0, top], [0, top], color=BASELINE, linewidth=1)
    for i, (name, cal) in enumerate(cals.items()):
        ax.plot(
            cal["predicted"],
            cal["observed"],
            "-o",
            color=SERIES[i],
            label=name,
            linewidth=2,
            markersize=8,
            markeredgecolor=SURFACE,
            markeredgewidth=2,
        )
    ax.set_xlim(0, top)
    ax.set_ylim(0, top)
    ax.legend(loc="upper left")
    ax.xaxis.set_major_formatter(PCT)
    ax.yaxis.set_major_formatter(PCT)
    _frame(ax, title, "y", "predicted revert probability", "observed revert rate")


def small_multiples(tables: dict[str, pd.Series], title: str, fmt: str = "{:.1%}", ncols: int = 3):
    """One single-series bar panel per table, a shared figure title."""
    nrows = -(-len(tables) // ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(4 * ncols, 2.6 * nrows))
    flat = np.atleast_1d(axes).ravel()
    for ax, (name, s) in zip(flat, tables.items(), strict=False):
        hbars(ax, s, name, fmt)
        ax.set_xticks([])
    for ax in flat[len(tables) :]:
        ax.set_visible(False)
    fig.suptitle(title, x=0.01, ha="left", fontweight="bold", color=INK)
    fig.tight_layout()
    return fig
