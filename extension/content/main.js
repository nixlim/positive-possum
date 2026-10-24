// Entry point: articles (namespace 0) only, data from the service worker, then banner and drawer.
const NAMESPACES = new Set([
  "media", "special", "talk", "user", "user talk", "wikipedia", "wikipedia talk", "wp", "wt",
  "project", "project talk", "file", "file talk", "image", "image talk", "mediawiki", "mediawiki talk",
  "template", "template talk", "help", "help talk", "category", "category talk", "portal", "portal talk",
  "draft", "draft talk", "mos", "mos talk", "timedtext", "timedtext talk", "module", "module talk",
  "event", "event talk", "gadget", "gadget talk", "gadget definition", "gadget definition talk", "topic",
]);

function articleTitle(pathname) {
  if (!pathname.startsWith("/wiki/")) return null;
  let title;
  try {
    title = decodeURIComponent(pathname.slice("/wiki/".length)).replace(/_/g, " ").trim();
  } catch {
    return null;
  }
  const colon = title.indexOf(":");
  if (colon > 0 && NAMESPACES.has(title.slice(0, colon).trim().toLowerCase())) return null;
  return title || null;
}

const POLL_MS = 10 * 1000;
const POLL_LIMIT = (30 * 60 * 1000) / POLL_MS;

(async function main() {
  const heading = document.getElementById("firstHeading");
  const title = articleTitle(location.pathname);
  if (!heading || !title) return;
  const send = (msg) => chrome.runtime.sendMessage(msg);
  const [pageRes, statusRes] = await Promise.all([send({ type: "page", title }), send({ type: "status" })]);
  if (pageRes && pageRes.error === "backend unreachable") mountNotice(heading, pageRes);
  let drawer = null;
  let classified = null;
  let polls = 0;
  let timer = null;
  let unloaded = false;
  window.addEventListener("pagehide", () => {
    unloaded = true;
    clearTimeout(timer);
  });
  // Mounts (or replaces) the banner and drawer for one PageAnalysis, keeping the drawer's state.
  // Polls while edits are queued; stops at 0, when the backend stopped the queue, or after 30 min.
  function mount(res) {
    const wasOpen = drawer ? drawer.isOpen() : false;
    const tab = drawer ? drawer.tab() : undefined;
    for (const id of ["edit-watch-banner", "edit-watch-drawer"]) document.getElementById(id)?.remove();
    const ctx = { title, send, status: statusRes, reload, onClassified };
    drawer = mountDrawer(res, ctx, tab);
    mountBanner(heading, res, () => drawer.toggle(true), classified);
    drawer.toggle(wasOpen);
    clearTimeout(timer);
    const page = res.data;
    if (!unloaded && page.classify_pending > 0 && !page.classify_stopped && polls < POLL_LIMIT) {
      timer = setTimeout(poll, POLL_MS);
    }
  }
  // Progress while the backend classifies: re-read past the worker's page cache. A failed read stops.
  async function poll() {
    polls += 1;
    const res = await send({ type: "page", title, fresh: true });
    if (res && res.data && !unloaded) mount(res);
  }
  async function reload() {
    polls = 0;
    const res = await send({ type: "page", title });
    if (res && res.data) mount(res);
  }
  async function onClassified(text) {
    classified = text;
    await reload();
  }
  function start(res) {
    mount(res);
    chrome.runtime.onMessage.addListener((msg) => {
      if (msg.type === "toggle") drawer.toggle();
    });
  }
  // First visit: `ingest` (the worker sends it once per title per session), then each
  // "Try again" click sends one `refresh`. A 404 means Wikipedia has no such article: no retry.
  async function firstVisit(type) {
    mountLine(heading, "reading this article's history from Wikipedia…");
    const res = await send({ type, title });
    if (res && res.data) {
      document.getElementById("edit-watch-line")?.remove();
      start(res);
      return;
    }
    mountLine(heading, refreshFailure(res), res && res.status === 404 ? null : () => firstVisit("refresh"));
  }
  if (pageRes && pageRes.status === 404) await firstVisit("ingest");
  else if (pageRes && pageRes.data) start(pageRes);
})();
