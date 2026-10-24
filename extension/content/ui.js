// Banner and drawer, each in a closed Shadow DOM so Wikipedia's CSS and scripts cannot reach them.
const TABS = [
  ["Pulse", renderPulse],
  ["Flags", renderFlags],
  ["Editors", renderEditors],
  ["Structure", renderStructure],
  ["Brief", renderBrief],
  ["Mubit", renderMubit],
];

function shadowHost(id) {
  const host = h("div", { id });
  const root = host.attachShadow({ mode: "closed" });
  root.append(h("style", { text: STYLE }));
  return { host, root };
}

function offlineMark(res) {
  return res.offline ? h("span", { class: "chip offline", title: res.reason, text: "offline · fixture data" }) : null;
}

function riskChip(page) {
  const text = "risk " + Math.round(page.risk_score) + (page.risk_partial ? " · partial" : "");
  return h("span", { class: "chip risk " + riskBand(page.risk_score), text });
}

function bannerFacts(page) {
  const a = page.analytics;
  const facts = [
    a.edits_30d + " edits / 30d",
    page.risk_partial ? "flags pending" : a.flagged_30d + " flagged",
    a.reverted_30d + " reverted",
    Math.min(3, a.editors_90d) + " editors made " + pct(a.top3_share),
  ];
  const move = controversyMove(page.structure);
  return move ? facts.concat(move) : facts;
}

// Vector 2022 puts the heading in a flex title bar; go below the bar, not beside the heading.
function placeUnder(heading, host) {
  (heading.closest(".vector-page-titlebar") || heading).after(host);
}

// One line when the backend cannot be reached: no data, no score, no colour band.
function mountNotice(heading, res) {
  const { host, root } = shadowHost("edit-watch-notice");
  root.append(
    h(
      "div",
      { class: "banner notice" },
      "Edit watch: no backend at " + res.backend + ". Start it with ",
      h("code", { text: "uv run triage api" }),
      ", or turn on demo mode in Options.",
    ),
  );
  placeUnder(heading, host);
}

function mountBanner(heading, res, onOpen) {
  const page = res.data;
  const { host, root } = shadowHost("edit-watch-banner");
  root.append(
    h(
      "div",
      { class: "banner " + riskBand(page.risk_score), title: "Signals for human review, not findings." },
      riskChip(page),
      h("span", { class: "facts", text: bannerFacts(page).join(" · ") }),
      offlineMark(res),
      h("button", { text: "Open", onclick: onOpen }),
    ),
  );
  placeUnder(heading, host);
}

function mountDrawer(res, ctx) {
  const page = res.data;
  const { host, root } = shadowHost("edit-watch-drawer");
  const body = h("div", { class: "body" });
  const nav = h("nav");
  const views = new Map();
  const panel = h("aside", { class: "drawer", hidden: "" });

  function toggle(open = panel.hidden) {
    panel.hidden = !open;
  }

  function show(name) {
    for (const b of nav.children) b.classList.toggle("on", b.dataset.tab === name);
    if (!views.has(name)) views.set(name, TABS.find(([t]) => t === name)[1](page, ctx));
    body.replaceChildren(views.get(name));
  }

  for (const [name] of TABS) nav.append(h("button", { "data-tab": name, text: name, onclick: () => show(name) }));
  panel.append(
    h(
      "header",
      null,
      h(
        "div",
        { class: "row" },
        h("h1", null, "Edit ", h("b", { text: "watch" })),
        riskChip(page),
        offlineMark(res),
        h("button", { class: "close", title: "close (Esc)", text: "×", onclick: () => toggle(false) }),
      ),
      h("div", { class: "page-title", title: page.title, text: page.title }),
      note("Signals for human review, not findings. Labels describe edits, not editors."),
    ),
    nav,
    body,
    footerView(ctx.status),
  );
  root.append(panel);
  document.body.append(host);
  show("Pulse");
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") toggle(false);
  });
  return { toggle, isOpen: () => !panel.hidden };
}
