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
  (cached brief or `404`), `GET /api/mubit/status`, and the only three `POST`s, `/api/page/brief`
  (generate a brief, charged to the Mubit budget, `429` when spent), `/api/page/refresh`
  (ingest or re-ingest from Wikipedia, `429` with `reason` `recent` or `wikipedia`) and
  `/api/page/classify` (queue the window's open edits through Mubit,
  `202 {queued, already, errored, pending, skipped, remaining}`, `429` when the budget is spent).
  No other request is a `POST`. Chrome adds the extension's `Origin` to the POSTs, which the API
  allowlists (`--extension-origin chrome-extension://<id>`). A non-2xx reply passes on the body's
  `reason` (top level, or `detail.reason` as FastAPI's `HTTPException` puts it; `null` without a
  JSON body). A 60 s in-memory cache for GETs (a successful POST drops the matching GET entry; a
  page read with `fresh: true` skips the cache but still stores the reply), the bundled fixtures
  in demo mode only (an unreachable backend replies `{data: null, error: "backend unreachable"}`,
  never fixture data), and the per-tab badge (colour by `risk_score`: green < 30, amber 30–60,
  red > 60, text `flagged_30d`; grey `–` with no data and while `risk_partial`, whatever the
  score). The worker owns the first-visit rule: an `ingest` message sends one
  `POST /api/page/refresh` per title per browser session, whatever the outcome, and only when its
  last `page` reply for that title was a `404` from a reachable backend; otherwise it replies
  `{data: null, error: "not ingesting"}` without a request. The title goes into an in-memory set
  before any `await`, then into `chrome.storage.session` before the `POST`, so two tabs asking at
  once send one `POST` and a restarted worker still finds the title (and replies with the stored
  outcome). A `refresh` message ("Try again", "Refresh from Wikipedia") always sends one `POST`.
  Clicking the toolbar icon opens or closes the drawer.
- `content/`: content scripts, loaded in order from the manifest. `main.js` skips non-article
  pages (namespace prefixes, no `#firstHeading`); `ui.js` mounts the banner under the heading and
  the right-hand drawer, both in closed Shadow DOM, or only the one-line notice when the backend
  is unreachable (never an ingest; demo mode never ingests either). On a first visit (`page`
  replies `404`) it shows "Edit watch: reading this article's history from Wikipedia…" and sends
  `ingest`; a `PageAnalysis` back mounts the banner and drawer, and a failure replaces the line
  with the reason, with no score, drawer or badge colour: `404` "Not covered: not an article
  Wikipedia knows"; `429` `recent` "Refreshed in the last 10 minutes; try again later"; `429`
  `wikipedia` "Not read: Wikipedia request budget spent"; anything else "Could not reach the
  triage backend". Every failure but the `404` adds "Try again", and each click sends one
  `refresh`. The banner reads "N flagged", or "N flagged so far (partial)" while `risk_partial`
  (the Flags tab then also says "recent edits not yet classified"), adds "K could not be
  classified" when `classify_errored > 0` and the reason when `classify_stopped` is set. While
  `classify_pending > 0` the page is re-read every 10 s with `fresh: true` (stopping at 0, when
  `classify_stopped` is set, when a read fails, on unload, or after 30 min), the banner and
  drawer are re-mounted keeping the open tab and drawer state, and the banner shows
  "classifying: N edits left". `tabs.js` renders Pulse, Flags, Editors and Structure from
  `PageAnalysis` (Pulse has "Refresh from Wikipedia"; Flags shows forced reviews and, only while
  `classify_open > 0`, "Classify recent edits" with its cost: at most four Mubit calls per queued
  edit per click, extract, classify and one retry of each. That button is the only sender of
  `classify`; after a `202` the banner shows the reply's counts, and a `429` says the Mubit call
  budget is spent);
  `mubit.js` renders Brief (cached, or "Request brief"; a safe markdown subset) and Mubit status
  (error groups, shift signals, repeat-run agreement), plus the per-step footer ("provider", or a
  highlighted "changed?" when a hard signal exists); `dom.js` builds every node with `createElement` and
  `textContent`; `style.js` holds the CSS.
- `shared.js`: default settings and the risk colour bands, used by all three contexts.
- `options.html`, `options.js`: settings in `chrome.storage.sync`.
- `fixtures/`: `page.json` (`PageAnalysis`), `brief.json`, `status.json`, `watchlist.json`, shaped
  like the API contract in `PIPELINE.md` section 9. Synthetic data, no real people.

`tests/test_extension.py` checks the manifest, the fixtures' contract keys, that no script parses
HTML strings or evaluates code, and runs `node --check` on each script.
`tests/test_extension_worker.py` runs `background.js` against a stubbed chrome API, including
`chrome.storage.session` (`tests/extension_harness.js`), and `tests/test_extension_content.py`
runs the content scripts against a fake DOM (`tests/content_harness.js`). These tests need node:
without it they fail, they do not skip.
