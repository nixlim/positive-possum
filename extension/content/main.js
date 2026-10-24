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

(async function main() {
  const heading = document.getElementById("firstHeading");
  const title = articleTitle(location.pathname);
  if (!heading || !title) return;
  const send = (msg) => chrome.runtime.sendMessage(msg);
  const [pageRes, statusRes] = await Promise.all([send({ type: "page", title }), send({ type: "status" })]);
  if (!pageRes || !pageRes.data) return;
  let drawer = null;
  // Mounts (or, after a refresh, replaces) the banner and drawer for one PageAnalysis.
  function mount(res) {
    const wasOpen = drawer ? drawer.isOpen() : false;
    for (const id of ["edit-watch-banner", "edit-watch-drawer"]) document.getElementById(id)?.remove();
    const ctx = { title, send, status: statusRes, reload };
    drawer = mountDrawer(res, ctx);
    mountBanner(heading, res, () => drawer.toggle(true));
    drawer.toggle(wasOpen);
  }
  async function reload() {
    const res = await send({ type: "page", title });
    if (res && res.data) mount(res);
  }
  mount(pageRes);
  chrome.runtime.onMessage.addListener((msg) => {
    if (msg.type === "toggle") drawer.toggle();
  });
})();
