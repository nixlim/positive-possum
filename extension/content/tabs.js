// Drawer tabs built from PageAnalysis: Pulse, Flags, Editors, Structure.
const FORCED = {
  refs_removed: "references removed",
  net_words_removed: "at least 50 more words removed than added",
  controversy_words_removed: "words removed from a controversy section",
};
const PARTIAL = "recent edits not yet classified";
const CLASSIFY_COST = "at most four Mubit calls per queued edit per click: extract, classify and one retry of each";
const STOPPED = {
  budget: "classification stopped: Mubit call budget spent",
  wikipedia: "classification stopped: Wikipedia request budget spent",
};
const classifyingText = (page) => "classifying: " + page.classify_pending + " edits left";

// Why a refresh (or first-visit ingest) gave no PageAnalysis, from its status and `reason`.
function refreshFailure(res) {
  if (res && res.status === 404) return "Not covered: not an article Wikipedia knows";
  if (res && res.status === 429 && res.reason === "recent") return "Refreshed in the last 10 minutes; try again later";
  if (res && res.status === 429 && res.reason === "wikipedia") return "Not read: Wikipedia request budget spent";
  return "Could not reach the triage backend";
}

function pulseChart(days, bursts, flags) {
  const W = 400;
  const H = 120;
  const pad = 16;
  const max = Math.max(1, ...days.map((d) => d.count));
  const step = (W - 2 * pad) / Math.max(1, days.length - 1);
  const x = (i) => pad + i * step;
  const y = (c) => H - pad - (c / max) * (H - 2 * pad);
  const index = new Map(days.map((d, i) => [d.date, i]));
  const svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H, class: "pulse", role: "img", "aria-label": "edits per day" });
  for (const date of bursts) {
    const i = index.get(date);
    if (i == null) continue;
    const w = Math.max(3, step);
    svg.append(svgEl("rect", { x: x(i) - w / 2, y: pad / 2, width: w, height: H - pad, class: "burst" }, null));
  }
  svg.append(svgEl("polyline", { points: days.map((d, i) => x(i) + "," + y(d.count)).join(" "), class: "line" }));
  for (const f of flags) {
    const i = index.get(f.timestamp.slice(0, 10));
    if (i == null) continue;
    const c = f.classification || {};
    const dot = svgEl("circle", { cx: x(i), cy: y(days[i].count), r: 3.5, class: "flag sev" + (c.severity ?? 0) });
    dot.append(svgEl("title", {}, labelName(c.label) + " · " + when(f.timestamp)));
    svg.append(dot);
  }
  if (days.length) {
    svg.append(svgEl("text", { x: pad, y: H - 3 }, days[0].date));
    svg.append(svgEl("text", { x: W - pad, y: H - 3, "text-anchor": "end" }, days[days.length - 1].date));
  }
  svg.append(svgEl("text", { x: pad, y: 11 }, "max " + max + " / day"));
  return svg;
}

function refreshControl(page, ctx) {
  const status = h("span", { class: "sub", text: "as of " + when(page.as_of) });
  const button = h("button", {
    text: "Refresh from Wikipedia",
    title: "re-ingest this page (at most once per 10 minutes)",
    onclick: async () => {
      button.disabled = true;
      status.textContent = "re-ingesting from Wikipedia…";
      const res = await ctx.send({ type: "refresh", title: ctx.title });
      if (res && res.data && !res.offline) {
        await ctx.reload();
        return;
      }
      status.textContent = res && res.offline ? "refresh needs the backend (offline)" : refreshFailure(res);
      button.disabled = false;
    },
  });
  return h("div", { class: "row refresh" }, button, status);
}

function renderPulse(page, ctx) {
  const a = page.analytics;
  const parts = Object.entries(page.risk_parts || {}).map(([k, v]) => [k.replace(/_/g, " "), "+" + v]);
  return h(
    "div",
    null,
    refreshControl(page, ctx),
    pulseChart(a.edits_per_day, a.burst_days, page.flags),
    note(
      "Edits per day over " + page.days + " days. Shaded: burst days (above mean + 2σ and at least 3). " +
        "Dots: flagged edits, coloured by severity.",
    ),
    kv([
      ["edits · 90d / 30d", a.edits_90d + " / " + a.edits_30d],
      ["editors · 90d", num(a.editors_90d)],
      ["share of edits by the top 3 editors", pct(a.top3_share)],
      ["anonymous (IP) share", pct(a.ip_share)],
      ["temporary account share", pct(a.temp_share)],
      ["new account share (< 30 days)", pct(a.new_account_share)],
      ["accounts editing mostly this page", num(a.spa_count)],
      ["reverted · 90d / 30d", a.reverted_90d + " / " + a.reverted_30d],
    ]),
    h("h3", { text: "risk " + Math.round(page.risk_score) + (page.risk_partial ? " · partial, pipeline not run" : "") }),
    kv(parts),
    note("Risk is a weighted sum of the signals above, for ordering pages to review. It is not a verdict."),
  );
}

function claims(extract) {
  if (!extract) return null;
  const lines = [
    ...extract.claims_added.map((t) => h("li", { class: "add", text: "+ " + t })),
    ...extract.claims_removed.map((t) => h("li", { class: "del", text: "− " + t })),
  ];
  return lines.length ? h("ul", { class: "claims" }, lines) : null;
}

function flagCard(e) {
  const c = e.classification || {};
  const m = e.measured || {};
  const url = safeDiffUrl(e.diff_url);
  const tone = labelTone(c.label);
  const props = url
    ? { class: "card " + tone, href: url, target: "_blank", rel: "noopener noreferrer", title: "open the diff on Wikipedia" }
    : { class: "card " + tone };
  const facts = [
    WHO[e.editor_kind] || e.editor_kind,
    signed(e.size_delta) + " bytes",
    "words +" + num(m.words_added) + " / −" + num(m.words_removed),
    "refs +" + num(m.refs_added) + " / −" + num(m.refs_removed),
    (m.sections || []).join(", ") || "(lead)",
  ];
  return h(
    url ? "a" : "div",
    props,
    h(
      "div",
      { class: "row" },
      h("span", { class: "chip " + tone, text: labelName(c.label) }),
      h("span", { class: "tag", text: "severity " + num(c.severity) }),
      c.review ? h("span", { class: "tag warn", text: "review" }) : null,
      UNSCORED.has(c.label)
        ? h("span", { class: "tag", title: "no automatic ground truth for this label", text: "unscored signal" })
        : null,
      e.reverted ? h("span", { class: "chip alert", text: "reverted" }) : null,
      h("span", { class: "time", text: when(e.timestamp) }),
    ),
    h("div", { class: "rationale", text: c.rationale || "(no rationale)" }),
    c.review_forced
      ? h("div", {
          class: "forced",
          text:
            "review forced by measured facts: " + (FORCED[c.review_forced] || c.review_forced.replace(/_/g, " ")) +
            (c.review_model === false ? " (the model said no review)" : ""),
        })
      : null,
    claims(e.extract),
    h("div", { class: "sub", text: facts.join(" · ") }),
    h("div", { class: "sub", text: "edit summary: " + (e.comment || "(none)") }),
  );
}

// The 202 reply's counts, each 30-day edit in exactly one of them.
function classifyCounts(d) {
  return [
    num(d.queued) + " queued",
    num(d.already) + " already classified",
    num(d.errored) + " errors",
    num(d.pending) + " already queued",
    num(d.skipped) + " not classified: Mubit budget",
    num(d.remaining) + " Mubit calls left",
  ].join(", ");
}

function classifyFailure(res) {
  if (res && res.offline) return "classifying needs the backend (offline)";
  if (res && res.status === 429 && res.reason === "wikipedia") return "classify failed: Wikipedia request budget spent";
  return "classify failed: " + errorText(res);
}

// Shown only while some edit can be queued (errored edits never are). The click is the only
// `classify` sender; the counts and progress go to the banner.
function classifyControl(page, ctx) {
  if (!(page.classify_open > 0)) return null;
  const status = h("span", { class: "sub", text: num(page.classify_open) + " edits to classify" });
  const button = h("button", {
    text: "Classify recent edits",
    onclick: async () => {
      button.disabled = true;
      status.textContent = "queueing…";
      const res = await ctx.send({ type: "classify", title: ctx.title });
      if (res && res.data && !res.offline) {
        await ctx.onClassified(classifyCounts(res.data));
        return;
      }
      status.textContent = classifyFailure(res);
      button.disabled = false;
    },
  });
  return h("div", { class: "refresh" }, note(CLASSIFY_COST), h("div", { class: "row" }, button, status));
}

function renderFlags(page, ctx) {
  const list = page.flags.length
    ? [
        note(
          "Edits flagged for human review. A label describes the edit, not the person who made it. " +
            "Click an edit to open its diff on Wikipedia.",
        ),
        ...page.flags.map(flagCard),
      ]
    : empty(page.risk_partial ? "No edits flagged so far." : "No edits flagged for review in this window.");
  return h("div", null, page.risk_partial ? note(PARTIAL) : null, classifyControl(page, ctx), list);
}

function renderEditors(page) {
  const heads = [
    ["account", null],
    ["kind", null],
    ["edits", "n"],
    ["share", "n"],
    ["age (d)", "n"],
    ["edit count", "n"],
    ["mostly here", "n"],
    ["reverted", "n"],
  ];
  const rows = page.editors.map((ed) =>
    h(
      "tr",
      null,
      h("td", { class: "name", title: ed.name, text: ed.name }),
      h("td", { text: WHO[ed.kind] || ed.kind }),
      h("td", { class: "n", text: num(ed.edits) }),
      h("td", { class: "n", text: pct(ed.share) }),
      h("td", { class: "n", text: num(ed.account_age_days) }),
      h("td", { class: "n", text: num(ed.edit_count) }),
      h("td", { class: "n", text: ed.spa ? "yes" : "" }),
      h("td", { class: "n", text: num(ed.reverted) }),
    ),
  );
  return h(
    "div",
    null,
    note("Who edited this page in the last " + page.days + " days. Counts describe activity, not people."),
    h(
      "table",
      null,
      h("thead", null, h("tr", null, heads.map(([t, cls]) => h("th", { class: cls, text: t })))),
      h("tbody", null, rows),
    ),
    note(
      "IP editors and temporary accounts are grouped into one row each. \"Mostly here\": at least 10 edits " +
        "here and at least 60% of the account's last 50 contributions on this page. Account age and edit " +
        "count are read at ingest, not at the time of each edit.",
    ),
  );
}

function sectionRow(title, before, after) {
  const delta = before != null && after != null ? after - before : null;
  return h(
    "tr",
    { class: CONTROVERSY.test(title) ? "controversy" : null },
    h("td", { text: title }),
    h("td", { class: "n", text: num(before) }),
    h("td", { class: "n", text: after == null ? "removed" : String(after) }),
    h("td", { class: "n " + (delta > 0 ? "grow" : delta < 0 ? "shrink" : ""), text: before == null ? "new" : signed(delta) }),
  );
}

function rankText(st) {
  const move = controversyMove(st);
  const then = st.controversy_rank_then;
  const now = st.controversy_rank_now;
  if (then == null && now == null) return "none found";
  const at = num(then) + " → " + num(now);
  return move ? at + " (" + move.replace(/^“.*” /, "") + ")" : at;
}

function renderStructure(page) {
  const st = page.structure;
  const then = st.sections_then;
  const now = st.sections_now;
  const before = new Map(then.sections.map((s) => [s.title, s.words]));
  const kept = new Set(now.sections.map((s) => s.title));
  const rows = now.sections.map((s) => sectionRow(s.title, before.get(s.title), s.words));
  for (const s of then.sections) if (!kept.has(s.title)) rows.push(sectionRow(s.title, s.words, null));
  const bloat = st.bloat_index == null ? "–" : (st.bloat_index > 0 ? "+" : "") + st.bloat_index.toFixed(2);
  return h(
    "div",
    null,
    note(
      "Sections at rev " + then.revid + " (" + when(then.timestamp) + ") against rev " + now.revid + " (" +
        when(now.timestamp) + "), words counted from the wikitext. Controversy sections are highlighted.",
    ),
    h(
      "table",
      null,
      h("thead", null, h("tr", null, h("th", { text: "section" }), ["then", "now", "Δ words"].map((t) => h("th", { class: "n", text: t })))),
      h("tbody", null, rows),
    ),
    kv([
      ["words", num(st.words_then) + " → " + num(st.words_now)],
      ["controversy section position", rankText(st)],
      ["controversy words", num(st.controversy_words_then) + " → " + num(st.controversy_words_now)],
      ["controversy share", pct(st.controversy_share_then) + " → " + pct(st.controversy_share_now)],
      ["bloat index", bloat],
      ["negative words removed", st.negative_words_removed == null ? "pipeline not run" : String(st.negative_words_removed)],
    ]),
    note(
      "Bloat index: growth outside the controversy sections, relative to the old article. Strong growth while " +
        "the controversy share falls and its section moves down is a pattern worth checking, not a finding.",
    ),
  );
}
