// Runs the extension's content scripts in a node vm with a minimal fake DOM and a stubbed
// chrome.runtime, then prints {hosts, value} as JSON: the Shadow DOM hosts mounted on the page
// (id and visible text) and the result of an optional async script run against the loaded scripts.
// Usage: node content_harness.js <extension dir> '<scenario json>'
// scenario: {path?, replies: {<message type>: reply}, script?: "<async function body>"}
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const [ext, raw] = process.argv.slice(2);
const scenario = JSON.parse(raw);
const NOT_FOUND = { data: null, offline: false, status: 404, error: "HTTP 404" };
const mounted = [];

class El {
  constructor(tag) {
    this.tag = tag;
    this.attrs = {};
    this.children = [];
    this.listeners = [];
  }
  get id() { return this.attrs.id; }
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

function find(node, tag) {
  if (typeof node !== "object") return null;
  if (node.tag === tag) return node;
  for (const c of node.children) {
    const hit = find(c, tag);
    if (hit) return hit;
  }
  return null;
}

const click = (node) => Promise.all(node.listeners.map((fn) => fn()));
const send = async (msg) => scenario.replies[msg.type] || NOT_FOUND;
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
const context = vm.createContext({ document, chrome, location, console, send, click, find, text });
const manifest = JSON.parse(fs.readFileSync(path.join(ext, "manifest.json"), "utf8"));
for (const file of manifest.content_scripts[0].js) {
  vm.runInContext(fs.readFileSync(path.join(ext, file), "utf8"), context, { filename: file });
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));
context.settle = settle;
(async () => {
  await settle();
  const value = scenario.script ? await vm.runInContext("(async () => {" + scenario.script + "})()", context) : null;
  const hosts = mounted.map((n) => ({ id: n.id, text: n.shadow ? text(n.shadow) : text(n) }));
  process.stdout.write(JSON.stringify({ hosts, value }));
})();
