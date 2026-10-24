// Runs extension/background.js in a node vm with a stubbed chrome API, sends it one message and
// prints {reply, badge, requests} as JSON, so tests can check the service worker without a browser.
// Usage: node extension_harness.js <extension dir> '<scenario json>'
// scenario: {settings: {...}, backend: "down" | {status, body}, message: {type, title}}
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const [ext, raw] = process.argv.slice(2);
const scenario = JSON.parse(raw);
const badge = {};
const requests = [];
const noop = { addListener() {} };
let onMessage = null;

const chrome = {
  storage: { sync: { get: async (defaults) => ({ ...defaults, ...scenario.settings }) }, onChanged: noop },
  runtime: {
    getURL: (p) => "bundled:" + p,
    onMessage: { addListener: (fn) => { onMessage = fn; } },
  },
  action: {
    setBadgeBackgroundColor: async ({ color }) => { badge.color = color; },
    setBadgeText: async ({ text }) => { badge.text = text; },
    onClicked: noop,
  },
  tabs: { sendMessage: async () => {} },
};

async function fetchStub(url, opts = {}) {
  if (url.startsWith("bundled:")) {
    const body = JSON.parse(fs.readFileSync(path.join(ext, url.slice("bundled:".length)), "utf8"));
    return { ok: true, status: 200, json: async () => body };
  }
  requests.push({ url, method: opts.method || "GET" });
  if (scenario.backend === "down") throw new TypeError("Failed to fetch");
  const { status, body } = scenario.backend;
  return { ok: status >= 200 && status < 300, status, json: async () => body };
}

const context = vm.createContext({ chrome, fetch: fetchStub, AbortSignal, console });
const run = (file) => vm.runInContext(fs.readFileSync(path.join(ext, file), "utf8"), context, { filename: file });
context.importScripts = (...files) => files.forEach(run);
run("background.js");
onMessage(scenario.message, { tab: { id: 1 } }, (reply) => {
  process.stdout.write(JSON.stringify({ reply, badge, requests }));
});
