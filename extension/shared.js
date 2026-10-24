// Shared by the service worker (importScripts), the content scripts and the options page.
const DEFAULT_SETTINGS = { backend: "http://127.0.0.1:8322", demo: false };

function loadSettings() {
  return chrome.storage.sync.get(DEFAULT_SETTINGS);
}

// Badge and banner colour band: grey no data, green < 30, amber 30-60, red > 60.
function riskBand(score) {
  if (score == null) return "none";
  if (score > 60) return "high";
  return score >= 30 ? "mid" : "low";
}
