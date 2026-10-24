// Runs the extension's content scripts in a node vm with a minimal fake DOM, a stubbed
// chrome.runtime and fake timers, then prints {hosts, sent, value} as JSON: the Shadow DOM hosts
// mounted on the page (id and visible text), every message sent to the service worker, and the
// result of an optional async script run against the loaded scripts.
// Usage: node content_harness.js <extension dir> '<scenario json>'
// scenario: {path?, replies: {<message type>: reply | [reply, ...] | "pending"}, script?: "<async function body>"}
// A list of replies answers in sequence and repeats its last; "pending" never answers.
// Scripts can use tick(ms) to run the fake timers, fire(type) for window events, and the DOM helpers.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const [ext, raw] = process.argv.slice(2);
const scenario = JSON.parse(raw);
const NOT_FOUND = { data: null, offline: false, status: 404, error: "HTTP 404" };
const mounted = [];
const sent = [];

class El {
  constructor(tag) {
    this.tag = tag;
    this.attrs = {};
    this.children = [];
    this.listeners = [];
    this.className = "";
  }
  get id() { return this.attrs.id; }
  get dataset() { return { tab: this.attrs["data-tab"] }; }
  get classList() {
    return {
      toggle: (name, on) => {
        const names = new Set(this.className.split(" ").filter(Boolean));
        if (on) names.add(name);
        else names.delete(name);
        this.className = [...names].join(" ");
      },
    };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  set textContent(v) { this.children = [String(v)]; }
  get textContent() { return text(this); }
  append(...nodes) { this.children.push(...nodes); }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(type, fn) { if (type === "click") this.listeners.push(fn); }
  attachShadow() { this.shadow = new El("#shadow-root"); return this.shadow; }
  closest() { return null; }
  after(node) { mounted.push(node); }
  remove() { mounted.splice(mounted.indexOf(this), 1); }
}

// Visible text: strings and elements, skipping <style>.
function text(node) {
  if (typeof node !== "object") return String(node);
  return node.tag === "style" ? "" : node.children.map(text).join("");
}

function find(node, tag, label) {
  if (typeof node !== "object") return null;
  if (node.tag === tag && (label == null || text(node) === label)) return node;
  for (const c of node.children) {
    const hit = find(c, tag, label);
    if (hit) return hit;
  }
  return null;
}

const click = (node) => Promise.all(node.listeners.map((fn) => fn()));
const send = async (msg) => {
  sent.push(msg);
  const reply = scenario.replies[msg.type];
  if (reply === "pending") return new Promise(() => {});
  if (Array.isArray(reply)) return reply.length > 1 ? reply.shift() : reply[0];
  return reply || NOT_FOUND;
};
const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

// Fake timers: tick(ms) runs every timer due within ms, in order, letting each one settle.
const timers = [];
let clock = 0;
let nextTimer = 1;
const fakeSetTimeout = (fn, ms) => {
  timers.push({ id: nextTimer, at: clock + ms, fn });
  return nextTimer++;
};
const fakeClearTimeout = (id) => {
  const i = timers.findIndex((t) => t.id === id);
  if (i >= 0) timers.splice(i, 1);
};
async function tick(ms) {
  const end = clock + ms;
  for (;;) {
    timers.sort((a, b) => a.at - b.at);
    if (!timers.length || timers[0].at > end) break;
    const t = timers.shift();
    clock = t.at;
    await t.fn();
    await settle();
  }
  clock = end;
}

const windowListeners = [];
const window = { addEventListener: (type, fn) => windowListeners.push({ type, fn }) };
const fire = (type) => windowListeners.filter((l) => l.type === type).forEach((l) => l.fn());

const heading = new El("h1");
heading.setAttribute("id", "firstHeading");
const body = new El("body");
body.append = (...nodes) => mounted.push(...nodes);
const document = {
  body,
  createElement: (tag) => new El(tag),
  createElementNS: (ns, tag) => new El(tag),
  getElementById: (id) => (id === "firstHeading" ? heading : mounted.find((n) => n.id === id) || null),
  addEventListener() {},
};
const chrome = { runtime: { sendMessage: send, onMessage: { addListener() {} } } };
const location = { pathname: scenario.path || "/wiki/Keir_Starmer" };
const context = vm.createContext({
  document,
  window,
  chrome,
  location,
  console,
  send,
  click,
  find,
  text,
  settle,
  tick,
  fire,
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
});
const manifest = JSON.parse(fs.readFileSync(path.join(ext, "manifest.json"), "utf8"));
for (const file of manifest.content_scripts[0].js) {
  vm.runInContext(fs.readFileSync(path.join(ext, file), "utf8"), context, { filename: file });
}

(async () => {
  await settle();
  const value = scenario.script ? await vm.runInContext("(async () => {" + scenario.script + "})()", context) : null;
  const hosts = mounted.map((n) => ({ id: n.id, text: n.shadow ? text(n.shadow) : text(n) }));
  process.stdout.write(JSON.stringify({ hosts, sent, value }));
})();
