// Service worker: the only place that talks to the backend. Content scripts ask by message,
// so Wikipedia's page never sees the backend URL and the badge is set per tab here. GETs read
// cached data; the two POSTs (brief generation, re-ingest) spend budget, and Chrome adds this
// extension's Origin to them, which the backend allowlists.
importScripts("shared.js");

const CACHE_MS = 60 * 1000;
const ROUTES = {
  page: { method: "GET", path: "/api/page", timeout: 15000, fixture: "fixtures/page.json" },
  brief: { method: "GET", path: "/api/page/brief", timeout: 15000, fixture: "fixtures/brief.json" },
  status: { method: "GET", path: "/api/mubit/status", timeout: 5000, fixture: "fixtures/status.json" },
  "brief-generate": { method: "POST", path: "/api/page/brief", timeout: 90000, fixture: "fixtures/brief.json", drops: "brief" },
  refresh: { method: "POST", path: "/api/page/refresh", timeout: 90000, fixture: "fixtures/page.json", drops: "page" },
};
const BADGE_COLOURS = { none: "#75829a", low: "#4ade80", mid: "#ffb547", high: "#ff5470" };
const cache = new Map();

function url(base, kind, title) {
  const query = kind === "status" ? "" : "?title=" + encodeURIComponent(title);
  return base.replace(/\/+$/, "") + ROUTES[kind].path + query;
}

// Demo mode only: a synthetic article must never stand in for the real one being read.
async function fixture(kind) {
  const r = await fetch(chrome.runtime.getURL(ROUTES[kind].fixture));
  return { data: await r.json(), offline: true, reason: "demo mode" };
}

async function fromBackend(base, kind, title) {
  const route = ROUTES[kind];
  const target = url(base, kind, title);
  const hit = route.method === "GET" && cache.get(target);
  if (hit && Date.now() - hit.at < CACHE_MS) return hit.value;
  const r = await fetch(target, { method: route.method, signal: AbortSignal.timeout(route.timeout) });
  if (!r.ok) return { data: null, offline: false, status: r.status, error: "HTTP " + r.status };
  const value = { data: await r.json(), offline: false, status: r.status };
  if (route.method === "GET") cache.set(target, { at: Date.now(), value });
  if (route.drops) cache.delete(url(base, route.drops, title));
  return value;
}

async function load(kind, title) {
  const { backend, demo } = await loadSettings();
  if (demo) return fixture(kind);
  try {
    return await fromBackend(backend, kind, title);
  } catch {
    return { data: null, offline: false, error: "backend unreachable", backend };
  }
}

async function setBadge(tabId, page) {
  const band = riskBand(page ? page.risk_score : null);
  await chrome.action.setBadgeBackgroundColor({ tabId, color: BADGE_COLOURS[band] });
  await chrome.action.setBadgeText({ tabId, text: band === "none" ? "–" : String(page.analytics.flagged_30d) });
}

chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!Object.hasOwn(ROUTES, msg.type)) return false;
  load(msg.type, msg.title)
    .then(async (res) => {
      if (msg.type === "page" && sender.tab) await setBadge(sender.tab.id, res.data);
      reply(res);
    })
    .catch((err) => reply({ data: null, offline: false, error: String(err) }));
  return true;
});

chrome.action.onClicked.addListener((tab) => {
  chrome.tabs.sendMessage(tab.id, { type: "toggle" }).catch(() => {});
});

chrome.storage.onChanged.addListener(() => cache.clear());
