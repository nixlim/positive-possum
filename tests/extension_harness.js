// Runs extension/background.js in a node vm with a stubbed chrome API, sends it messages in order
// and prints {reply, replies, badge, badges, requests, session} as JSON, so tests can check the
// service worker without a browser. `reply` is the last reply, `badge` the last badge set,
// `badges` the last one per tab and `session` what chrome.storage.session holds at the end.
// Each request also lists the session keys present when it was sent.
// Usage: node extension_harness.js <extension dir> '<scenario json>'
// scenario: {settings: {...}, session?: {...}, backend, message: msg | messages: [step, ...]}
// step: {type, title, fresh?, tab?} (tab id, default 1) | {advance: ms} | {together: [msg, ...]}
// (sent at once, before any reply) | {restart: true} (a fresh worker; session storage is kept).
// backend: "down" | {status, body} (every request) | {"<METHOD> <path>": [answer, ...]}, where an
// answer is {status, body} or "down" (the request throws); a scripted route answers in sequence
// and repeats its last answer. An answer without a body has none that parses as JSON.
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const [ext, raw] = process.argv.slice(2);
const scenario = JSON.parse(raw);
const badge = {};
const badges = {};
const requests = [];
const replies = [];
const session = { ...scenario.session };
const noop = { addListener() {} };
const clock = { now: 1_000_000_000 };
const later = () => new Promise((resolve) => setTimeout(resolve, 0));
let onMessage = null;
let changeListeners = [];

const chrome = {
  storage: {
    sync: { get: async (defaults) => ({ ...defaults, ...scenario.settings }) },
    session: {
      get: async (key) => {
        await later();
        return key in session ? { [key]: session[key] } : {};
      },
      set: async (items) => {
        await later();
        const changes = Object.fromEntries(Object.entries(items).map(([k, v]) => [k, { newValue: v }]));
        Object.assign(session, JSON.parse(JSON.stringify(items)));
        changeListeners.forEach((fn) => fn(changes, "session"));
      },
    },
    onChanged: { addListener: (fn) => changeListeners.push(fn) },
  },
  runtime: {
    getURL: (p) => "bundled:" + p,
    onMessage: { addListener: (fn) => { onMessage = fn; } },
  },
  action: {
    setBadgeBackgroundColor: async ({ tabId, color }) => {
      badge.color = color;
      badges[tabId] = { ...badges[tabId], color };
    },
    setBadgeText: async ({ tabId, text }) => {
      badge.text = text;
      badges[tabId] = { ...badges[tabId], text };
    },
    onClicked: noop,
  },
  tabs: { sendMessage: async () => {} },
};

function answer(method, url) {
  const backend = scenario.backend;
  if ("status" in backend) return backend;
  const key = method + " " + new URL(url).pathname;
  const queue = backend[key];
  if (!queue) throw new Error("no scripted answer for " + key);
  return queue.length > 1 ? queue.shift() : queue[0];
}

async function fetchStub(url, opts = {}) {
  if (url.startsWith("bundled:")) {
    const body = JSON.parse(fs.readFileSync(path.join(ext, url.slice("bundled:".length)), "utf8"));
    return { ok: true, status: 200, json: async () => body };
  }
  const method = opts.method || "GET";
  requests.push({ url, method, session: Object.keys(session) });
  await later();
  const reply = scenario.backend === "down" ? "down" : answer(method, url);
  if (reply === "down") throw new TypeError("Failed to fetch");
  const { status, body } = reply;
  const json = async () => {
    if (body === undefined) throw new SyntaxError("Unexpected end of JSON input");
    return body;
  };
  return { ok: status >= 200 && status < 300, status, json };
}

// A fresh worker: new globals, no in-memory state, only storage carried over.
function start() {
  onMessage = null;
  changeListeners = [];
  const context = vm.createContext({ chrome, fetch: fetchStub, AbortSignal, console, clock });
  vm.runInContext("Date.now = () => clock.now;", context);
  const run = (file) => vm.runInContext(fs.readFileSync(path.join(ext, file), "utf8"), context, { filename: file });
  context.importScripts = (...files) => files.forEach(run);
  run("background.js");
}

const ask = (msg) => new Promise((resolve) => onMessage(msg, { tab: { id: msg.tab ?? 1 } }, resolve));

start();
(async () => {
  for (const step of scenario.messages || [scenario.message]) {
    if (step.advance) clock.now += step.advance;
    else if (step.restart) start();
    else if (step.together) replies.push(...(await Promise.all(step.together.map(ask))));
    else replies.push(await ask(step));
  }
  const out = { reply: replies[replies.length - 1], replies, badge, badges, requests, session };
  process.stdout.write(JSON.stringify(out));
})();
