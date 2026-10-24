// Drawer tabs that come from the backend's other endpoints: Brief (cached, or generated on request)
// and Mubit status, plus the per-step footer shown on every tab.
const STEPS = [
  ["edit-extract", "extract"],
  ["edit-classify", "classify"],
  ["page-brief", "brief"],
];
const AGREEMENTS = [
  ["extract", "extract (identical input)"],
  ["classify", "classify (identical input)"],
  ["chained", "drift through the chain (not identical input)"],
];

// A tiny markdown subset: headings, paragraphs and lists. Everything else stays literal text.
function markdown(src) {
  const root = h("div", { class: "md" });
  let para = [];
  let list = null;
  const flush = () => {
    if (para.length) root.append(h("p", { text: para.join(" ") }));
    para = [];
  };
  for (const raw of String(src || "").split(/\r?\n/)) {
    const line = raw.trim();
    const heading = /^(#{1,6})\s+(.*)$/.exec(line);
    const item = /^([-*+]|\d+[.)])\s+(.*)$/.exec(line);
    if (!item) list = null;
    if (!line || heading || item) flush();
    if (heading) {
      root.append(h(heading[1].length <= 2 ? "h3" : "h4", { text: heading[2] }));
    } else if (item) {
      const tag = /^\d/.test(item[1]) ? "ol" : "ul";
      if (!list || list.tagName.toLowerCase() !== tag) {
        list = h(tag);
        root.append(list);
      }
      list.append(h("li", { text: item[2] }));
    } else if (line) {
      para.push(line);
    }
  }
  flush();
  return root;
}

function errorText(res) {
  if (res && res.status === 429) return "Mubit call budget for this session is spent";
  return (res && res.error) || "no data";
}

function briefView(res) {
  if (!res || !res.data) return empty("No brief: " + errorText(res));
  const b = res.data;
  return h(
    "div",
    null,
    markdown(b.brief_md),
    note(
      "generated " + when(b.generated_at) + " · served by " + (b.model_served || "unknown") +
        (res.offline ? " · offline, fixture data" : ""),
    ),
  );
}

function renderBrief(page, ctx) {
  const out = h("div", null, note("looking for a cached brief…"));
  const button = h("button", {
    text: "Request brief",
    hidden: "",
    onclick: async () => {
      button.disabled = true;
      out.replaceChildren(note("writing the brief through Mubit, about 30 s…"));
      const res = await ctx.send({ type: "brief-generate", title: ctx.title });
      out.replaceChildren(briefView(res));
      button.hidden = Boolean(res && res.data);
      button.disabled = false;
    },
  });
  ctx.send({ type: "brief", title: ctx.title }).then((res) => {
    if (res && res.status === 404) {
      button.hidden = false;
      out.replaceChildren(empty("No brief for this page yet."));
    } else {
      out.replaceChildren(briefView(res));
    }
  });
  return h(
    "div",
    null,
    note(
      "A short analyst brief written by the page-brief step from the statistics and flagged edits. " +
        "Requesting one spends a Mubit call; the backend caches it afterwards.",
    ),
    button,
    out,
  );
}

const fmt = (v) => (Array.isArray(v) ? v.join(", ") || "none" : v == null ? "–" : String(v));
const yesNo = (v) => (v == null ? "–" : v ? "yes" : "no");

function hardSignal(s) {
  return h(
    "li",
    { class: "alert" },
    "hard · " + num(s.count) + " call(s) since " + when(s.first_ts) + " · changed: " + fmt(s.changed),
    h("div", {
      class: "sub",
      text:
        "served " + fmt(s.served) + " · provider headers " + yesNo(s.provider_headers) + " · id prefix " +
        fmt(s.id_prefix) + " · usage keys " + fmt(s.usage_keys),
    }),
  );
}

function softSignal(s) {
  return h("li", {
    class: "warn",
    text:
      "soft · " + fmt(s.metric) + " " + fmt(s.direction) + ": recent median " + fmt(s.recent_median) +
      " (n=" + num(s.n_recent) + ") against baseline " + fmt(s.baseline_median) + " (IQR " +
      fmt(s.baseline_iqr) + ")",
  });
}

function shiftView(shift) {
  const hard = (shift && shift.hard) || [];
  const soft = (shift && shift.soft) || [];
  if (!hard.length && !soft.length) return h("p", { class: "sub", text: "shift signals: none against the baseline" });
  return h("ul", { class: "shift" }, hard.map(hardSignal), soft.map(softSignal));
}

function agreementView(agreement) {
  const rows = AGREEMENTS.filter(([key]) => agreement && agreement[key]).map(([key, name]) => [
    name,
    pct(agreement[key].value) + " of n=" + num(agreement[key].n),
  ]);
  return [
    h("h4", { text: "Repeat-run agreement (answered in both runs)" }),
    rows.length ? kv(rows) : h("p", { class: "sub", text: "no repeat runs yet" }),
  ];
}

function stepCard(name, s) {
  if (!s) return h("div", { class: "card" }, h("h3", { text: name }), empty("no calls logged yet"));
  const served = Object.entries(s.served_by || {}).sort((a, b) => b[1] - a[1]);
  const errors = s.errors || {};
  const consoleRows = Object.entries(s.console || {}).map(([k, v]) => [k.replace(/_/g, " "), fmt(v)]);
  return h(
    "div",
    { class: "card" },
    h("h3", { text: name }),
    h(
      "ul",
      { class: "served" },
      served.map(([model, n]) => h("li", null, h("span", { class: "tag", text: model }), " " + n + " calls · " + pct(n / s.answered))),
    ),
    kv([
      ["requests · answered", num(s.requests) + " · " + num(s.answered)],
      ["errors · gateway / unparseable", num(errors.gateway) + " / " + num(errors.unparseable)],
      ["provider share", pct(s.provider_share)],
      ["latency p50", num(s.latency_p50_ms) + " ms"],
      ["tokens in · out", num(s.tokens_in) + " · " + num(s.tokens_out)],
    ]),
    shiftView(s.shift),
    agreementView(s.agreement),
    consoleRows.length ? [h("h4", { text: "Mubit console (typed by hand)" }), kv(consoleRows)] : null,
  );
}

function renderMubit(page, ctx) {
  const status = ctx.status && ctx.status.data;
  if (!status) return empty("Mubit status unavailable.");
  return h(
    "div",
    null,
    note(
      "Who answered each step, read from our own call log (served model string and headers). Errored calls " +
        "are counted, never scored. Mubit has no read API, so the console fields are copied by hand." +
        (ctx.status.offline ? " Offline: fixture data." : ""),
    ),
    STEPS.map(([key, name]) => stepCard(name, status[key])),
  );
}

function servedSummary(s) {
  const served = Object.entries((s && s.served_by) || {}).sort((a, b) => b[1] - a[1]);
  if (!served.length) return "no answered calls";
  const total = served.reduce((n, [, v]) => n + v, 0);
  return served.map(([model, n]) => model + " " + pct(n / total)).join(", ");
}

// "provider" while there is no hard signal and every answered call had the provider fingerprint.
function stepState(s) {
  if (!s || !s.answered) return { text: "no calls" };
  if (s.shift && s.shift.hard && s.shift.hard.length) return { text: "changed?", cls: "changed" };
  return { text: s.provider_share === 1 ? "provider" : servedSummary(s) };
}

function footerView(statusRes) {
  const status = statusRes && statusRes.data;
  if (!status) return h("footer", { text: "Mubit status unavailable" });
  const parts = STEPS.map(([key, name], i) => {
    const state = stepState(status[key]);
    return [
      i ? " · " : null,
      name + ": ",
      h("span", { class: state.cls, title: "served by " + servedSummary(status[key]), text: state.text }),
    ];
  });
  return h("footer", null, parts, statusRes.offline ? " · offline" : null);
}
