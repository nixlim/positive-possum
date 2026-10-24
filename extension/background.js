// Service worker: the only place that talks to the backend. Content scripts ask by message,
// so Wikipedia's page never sees the backend URL and the badge is set per tab here. GETs read
// cached data; the three POSTs (brief generation, ingest or re-ingest, classification) spend
// budget, and Chrome adds this extension's Origin to them, which the backend allowlists.
importScripts("shared.js");

const CACHE_MS = 60 * 1000;
const ROUTES = {
  page: { method: "GET", path: "/api/page", timeout: 15000, fixture: "fixtures/page.json" },
  brief: { method: "GET", path: "/api/page/brief", timeout: 15000, fixture: "fixtures/brief.json" },
  status: { method: "GET", path: "/api/mubit/status", timeout: 5000, fixture: "fixtures/status.json" },
  "brief-generate": { method: "POST", path: "/api/page/brief", timeout: 90000, fixture: "fixtures/brief.json", drops: "brief" },
  refresh: { method: "POST", path: "/api/page/refresh", timeout: 90000, fixture: "fixtures/page.json", drops: "page" },
  classify: { method: "POST", path: "/api/page/classify", timeout: 15000, fixture: "fixtures/page.json", drops: "page" },
};
const BADGE_COLOURS = { none: "#75829a", low: "#4ade80", mid: "#ffb547", high: "#ff5470" };
const cache = new Map();
// First visit: titles whose last page reply was a backend 404, and the one ingest each gets per
// browser session (its reply, in memory; its record, in chrome.storage.session across restarts).
const missing = new Set();
const ingests = new Map();
const NOT_INGESTING = { data: null, offline: false, error: "not ingesting" };

function url(base, kind, title) {
  const query = kind === "status" ? "" : "?title=" + encodeURIComponent(title);
  return base.replace(/\/+$/, "") + ROUTES[kind].path + query;
}

// Demo mode only: a synthetic article must never stand in for the real one being read.
async function fixture(kind) {
  const r = await fetch(chrome.runtime.getURL(ROUTES[kind].fixture));
  return { data: await r.json(), offline: true, reason: "demo mode" };
}

// Why the backend refused: `{reason}`, or `{detail: {reason}}` as FastAPI's HTTPException sends it.
async function reasonOf(r) {
  try {
    const body = await r.json();
    return body.reason ?? body.detail?.reason ?? null;
  } catch {
    return null;
  }
}

// `fresh` skips the cache read (progress polls) but still stores the reply.
async function fromBackend(base, kind, title, fresh) {
  const route = ROUTES[kind];
  const target = url(base, kind, title);
  const hit = !fresh && route.method === "GET" && cache.get(target);
  if (hit && Date.now() - hit.at < CACHE_MS) return hit.value;
  const r = await fetch(target, { method: route.method, signal: AbortSignal.timeout(route.timeout) });
  if (!r.ok) return { data: null, offline: false, status: r.status, error: "HTTP " + r.status, reason: await reasonOf(r) };
  const value = { data: await r.json(), offline: false, status: r.status };
  if (route.method === "GET") cache.set(target, { at: Date.now(), value });
  if (route.drops) cache.delete(url(base, route.drops, title));
  return value;
}

async function load(kind, title, fresh) {
  const { backend, demo } = await loadSettings();
  if (demo) return fixture(kind);
  try {
    return await fromBackend(backend, kind, title, fresh);
  } catch {
    return { data: null, offline: false, error: "backend unreachable", backend };
  }
}

// Only a 404 from a reachable backend starts an ingest, once per title per browser session
// whatever the outcome. The in-memory record is checked and made before any await, so two tabs
// asking at once share one POST; session storage keeps the record when the worker restarts.
function ingest(title) {
  if (!missing.has(title)) return Promise.resolve(NOT_INGESTING);
  if (!ingests.has(title)) ingests.set(title, ingestOnce(title));
  return ingests.get(title);
}

async function ingestOnce(title) {
  const key = "ingest:" + title;
  const seen = (await chrome.storage.session.get(key))[key];
  if (seen) return seen;
  await chrome.storage.session.set({ [key]: { data: null, offline: false, error: "ingest already sent" } });
  const res = await load("refresh", title);
  await chrome.storage.session.set({ [key]: { ...res, data: null } });
  return res;
}

// Grey while the window is partial: a partial flagged_30d would understate the score.
async function setBadge(tabId, page) {
  const band = page && !page.risk_partial ? riskBand(page.risk_score) : "none";
  await chrome.action.setBadgeBackgroundColor({ tabId, color: BADGE_COLOURS[band] });
  await chrome.action.setBadgeText({ tabId, text: band === "none" ? "–" : String(page.analytics.flagged_30d) });
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  const isIngest = msg.type === "ingest";
  if (!isIngest && !Object.hasOwn(ROUTES, msg.type)) return false;
  (isIngest ? ingest(msg.title) : load(msg.type, msg.title, msg.fresh))
    .then(async (res) => {
      if (msg.type === "page") {
        if (res.status === 404) missing.add(msg.title);
        else missing.delete(msg.title);
      }
      const isPage = msg.type === "page" || isIngest || (msg.type === "refresh" && res.data);
      if (isPage && sender.tab) await setBadge(sender.tab.id, res.data);
      reply(res);
    })
    .catch((err) => reply({ data: null, offline: false, error: String(err) }));
  return true;
});

chrome.action.onClicked.addListener((tab) => {
  chrome.tabs.sendMessage(tab.id, { type: "toggle" }).catch(() => {});
});

// Settings live in sync storage; the worker's own session writes must not drop its state.
chrome.storage.onChanged.addListener((changes, area) => {
  if (area !== "sync") return;
  cache.clear();
  missing.clear();
});
