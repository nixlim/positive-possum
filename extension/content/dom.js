// DOM helpers for the content script. API strings reach the page only through textContent or
// setAttribute, never through an HTML parser.
const SVG_NS = "http://www.w3.org/2000/svg";
const WHO = { registered: "registered account", anonymous: "anonymous (IP)", temporary: "temporary account" };
const LABEL_TONE = {
  content: "ok",
  maintenance: "ok",
  sourcing: "ok",
  promotional: "warn",
  unsourced_claim: "warn",
  whitewash: "alert",
  smear: "alert",
  vandalism: "alert",
  unclear: "dim",
};
// Labels with no automatic ground truth: shown as signals, never scored.
const UNSCORED = new Set(["whitewash", "smear"]);
const CONTROVERSY =
  /controvers|criticism|legal|allegation|scandal|investigation|convict|expenses|suspen|misconduct|lobbying/i;

function h(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "onclick") node.addEventListener("click", v);
    else node.setAttribute(k, v);
  }
  for (const c of children.flat()) if (c != null && c !== false) node.append(c);
  return node;
}

function svgEl(tag, attrs, text) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  if (text != null) node.textContent = text;
  return node;
}

function kv(rows) {
  return h(
    "dl",
    { class: "kv" },
    rows.flatMap(([k, v]) => [h("dt", { text: k }), h("dd", { text: v ?? "–" })]),
  );
}

const note = (text) => h("p", { class: "note", text });
const empty = (text) => h("p", { class: "empty", text });
const pct = (x) => (x == null ? "–" : Math.round(x * 100) + "%");
const num = (x) => (x == null ? "–" : String(x));
const signed = (n) => (n == null ? "–" : (n > 0 ? "+" : "") + n);
const when = (ts) => (ts ? ts.slice(0, 16).replace("T", " ") + " UTC" : "–");
const labelTone = (label) => LABEL_TONE[label] || "dim";
const labelName = (label) => (label ? label.replace(/_/g, " ") : "unclassified");

function safeDiffUrl(url) {
  return typeof url === "string" && url.startsWith("https://en.wikipedia.org/") ? url : null;
}

// "Expenses inquiry" moved down 3 places, or null when there is no move to report.
function controversyMove(st) {
  const then = st.controversy_rank_then;
  const now = st.controversy_rank_now;
  if (then == null || now == null || then === now) return null;
  const section = st.sections_now.sections[now];
  const n = Math.abs(now - then);
  return (
    "“" + (section ? section.title : "controversy section") + "” moved " +
    (now > then ? "down " : "up ") + n + (n === 1 ? " place" : " places")
  );
}
