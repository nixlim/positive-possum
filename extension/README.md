# Edit watch (Chrome extension)

Manifest V3, plain JS, no build step. Shows signals for human review about recent edits to the
English Wikipedia article you are reading, from the local API (`uv run triage api`, port 8322).

## Load it

1. `chrome://extensions` → turn on Developer mode → **Load unpacked** → pick this `extension/`
   directory.
2. Open any article on `https://en.wikipedia.org/wiki/…`. Without a backend running, the
   extension shows a one-line notice under the title ("no backend at …") and no data, and the
   badge stays grey. To see the bundled fixtures (a synthetic article, marked "offline · fixture
   data"), turn on demo mode in Options.
3. Options (right-click the toolbar icon → Options): backend URL (default
   `http://127.0.0.1:8322`) and demo mode (always use the fixtures, never call the backend).
   Only `127.0.0.1:8322` is in the host permissions; another backend must allow CORS.

## Parts

- `background.js`: service worker. All network calls: `GET /api/page`, `GET /api/page/brief`
  (cached brief or `404`), `GET /api/mubit/status`, and the only two `POST`s, `/api/page/brief`
  (generate a brief, charged to the Mubit budget, `429` when spent) and `/api/page/refresh`
  (re-ingest from Wikipedia, at most once per 10 min). Chrome adds the extension's `Origin` to the
  POSTs, which the API allowlists (`--extension-origin chrome-extension://<id>`). A 60 s in-memory
  cache for GETs (a successful POST drops the matching GET entry), the bundled fixtures in demo
  mode only (an unreachable backend replies `{data: null, error: "backend unreachable"}`, never
  fixture data), and the per-tab badge (colour by
  `risk_score`: grey no data, green < 30, amber 30–60, red > 60; text `flagged_30d`). Clicking the
  toolbar icon opens or closes the drawer.
- `content/`: content scripts, loaded in order from the manifest. `main.js` skips non-article
  pages (namespace prefixes, no `#firstHeading`); `ui.js` mounts the banner under the heading and
  the right-hand drawer, both in closed Shadow DOM, or only the one-line notice when the backend
  is unreachable (a `404` or other error shows nothing); `tabs.js` renders Pulse, Flags, Editors and
  Structure from `PageAnalysis` (Pulse has "Refresh from Wikipedia"; Flags shows forced reviews);
  `mubit.js` renders Brief (cached, or "Request brief"; a safe markdown subset) and Mubit status
  (error groups, shift signals, repeat-run agreement), plus the per-step footer ("provider", or a
  highlighted "changed?" when a hard signal exists); `dom.js` builds every node with `createElement` and
  `textContent`; `style.js` holds the CSS.
- `shared.js`: default settings and the risk colour bands, used by all three contexts.
- `options.html`, `options.js`: settings in `chrome.storage.sync`.
- `fixtures/`: `page.json` (`PageAnalysis`), `brief.json`, `status.json`, `watchlist.json`, shaped
  like the API contract in `PIPELINE.md` section 9. Synthetic data, no real people.

`tests/test_extension.py` checks the manifest, the fixtures' contract keys, that no script parses
HTML strings or evaluates code, and runs `node --check` on each script when node is installed.
With node, `tests/test_extension_worker.py` runs `background.js` against a stubbed chrome API
(`tests/extension_harness.js`) and `tests/test_extension_content.py` runs the content scripts
against a fake DOM (`tests/content_harness.js`).
