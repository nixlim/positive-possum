# EditWatch (Chrome extension)

Shows signals for human review about recent edits to the English Wikipedia article you are
reading. Manifest V3, plain JavaScript, no build step. All data comes from the local API
(`uv run triage api`, port 8322); see the [main README](../README.md) for starting it.

## Load it

1. Open `chrome://extensions`, turn on **Developer mode**, choose **Load unpacked** and pick this
   `extension/` directory.
2. Open any article on `https://en.wikipedia.org/wiki/…`.

With no backend running, the extension shows a one-line notice under the title ("no backend at
…"), no data, and a grey badge.

## Options

Right-click the toolbar icon and choose **Options**.

| Setting | Default | What it does |
| --- | --- | --- |
| Backend URL | `http://127.0.0.1:8322` | Where the API runs. Only this origin is in the host permissions; another backend must allow CORS. |
| Demo mode | off | Always show the bundled fixtures (a synthetic article, marked "offline · fixture data") and never call the backend. |

## What it shows

| Part | Where | Content |
| --- | --- | --- |
| Badge | Toolbar icon | Colour by risk score: green < 30, amber 30–60, red > 60. Text is the number of flagged edits in the last 30 days. Grey `–` with no data, or while some recent edits are not yet classified. |
| Banner | Under the article title | `23 edits / 30d · 4 flagged · 2 reverted · 3 editors made 80% · …` and an **Open** button. |
| Drawer | Right-hand panel, opened by **Open** or the toolbar icon | The six tabs below. |
| Footer | Bottom of the drawer | Per step, who answered: `provider`, or a highlighted `changed?` when a hard switch signal exists. |

| Tab | Shows | Mubit calls |
| --- | --- | --- |
| Pulse | Edits per day over 90 days, burst days, flagged edits; **Refresh from Wikipedia** | None |
| Flags | Classified edits (label, severity, rationale, account kind, reverted), forced reviews; **Classify recent edits** | On a click |
| Editors | Top editors and their share, IP, temporary and new accounts, single-purpose accounts | None |
| Structure | Sections then and now, controversy section rank, bloat index | None |
| Brief | The cached brief, or **Request brief** | On a click |
| Mubit | Per step: who served, provider share, error groups, shift signals, repeat-run agreement, console fields typed by hand | None |

Banner and drawer live in closed Shadow DOM, so the article's own styles and scripts cannot reach
them.

## How a visit goes

### First visit

When the API has no snapshot of the article (`GET /api/page` replies `404`), the banner reads
"EditWatch: reading this article's history from Wikipedia…" and the extension asks the API to
read it, once per title per browser session. This makes no Mubit calls. On success the banner and
drawer appear. On failure the line shows the reason, with no score, drawer or badge colour:

| Reply | Line | Try again |
| --- | --- | --- |
| `404` | Not covered: not an article Wikipedia knows | No |
| `429` `recent` | Refreshed in the last 10 minutes; try again later | Yes |
| `429` `wikipedia` | Not read: Wikipedia request budget spent | Yes |
| anything else | Could not reach the triage backend | Yes |

Each **Try again** click sends one refresh. Demo mode and an unreachable backend never ask for a
read.

### Classifying recent edits

**Classify recent edits** (Flags tab) appears while some of the window's edits can still be sent.
It is the only thing that sends edits to Mubit, at most four calls per edit per click (extract,
classify and one retry of each). After a click:

- the banner shows the reply's counts (queued, already done, errored, skipped for budget) until
  the queue finishes or stops;
- the page is re-read every 10 s, bypassing the cache, and the banner reads "classifying: N edits
  left". Polling stops when nothing is pending, when the queue stops, when a read fails, when the
  tab closes, or after 30 minutes;
- when the queue ends, the Mubit tab and footer re-read the Mubit status once.

Until every recent edit is classified the banner says "N flagged so far (partial)", the Flags tab
says "recent edits not yet classified", and the badge stays grey. Edits that could not be
classified are counted in the banner ("K could not be classified"), with the reason when the queue
stopped. A `429` means the Mubit call budget is spent.

### Requesting a brief

**Request brief** (Brief tab) spends one Mubit call. The API caches the brief, so the next visit
shows it without a call. Briefs are rendered from a safe markdown subset.

## Files

| File | Role |
| --- | --- |
| `background.js` | Service worker. Every network call, the GET cache, demo fixtures, the badge, and the first-visit rule. Clicking the toolbar icon opens or closes the drawer. |
| `content/main.js` | Skips non-article pages (namespace prefixes, no `#firstHeading`), drives first visit, polling and re-mounts. |
| `content/ui.js` | Mounts the notice, banner and drawer, keeping the open tab across re-mounts. |
| `content/tabs.js` | Pulse, Flags, Editors and Structure, from `PageAnalysis`. |
| `content/mubit.js` | Brief and Mubit tabs, and the footer. |
| `content/dom.js` | Builds every node with `createElement` and `textContent`; no HTML strings. |
| `content/style.js` | The CSS. |
| `shared.js` | Default settings and risk colour bands, shared by all three contexts. |
| `options.html`, `options.js` | Settings in `chrome.storage.sync`. |
| `fixtures/` | `page.json` (`PageAnalysis`), `brief.json`, `status.json`, `watchlist.json`, shaped like the API contract in `PIPELINE.md` section 9. Synthetic data, no real people. |

## Backend calls

All requests come from `background.js`.

| Request | When | Reply |
| --- | --- | --- |
| `GET /api/page` | Every article visit, and each 10 s poll | `PageAnalysis`, or `404` before the first read |
| `GET /api/page/brief` | Brief tab | Cached brief, or `404` |
| `GET /api/mubit/status` | Page load, and when a classify queue ends | Per-step status |
| `POST /api/page/refresh` | First visit, **Try again**, **Refresh from Wikipedia** | `PageAnalysis`; `429` with `reason` `recent` or `wikipedia` |
| `POST /api/page/classify` | **Classify recent edits** | `202 {queued, already, errored, pending, skipped, remaining}`; `429` when the budget is spent |
| `POST /api/page/brief` | **Request brief** | The brief; `429` when the budget is spent |

- **Only three POSTs.** Chrome adds the extension's `Origin` to them, which the API must allow
  (`--extension-origin chrome-extension://<id>`); without it they get `403`.
- **Errors.** A non-2xx reply passes on the body's `reason` (top level, or `detail.reason` as
  FastAPI's `HTTPException` puts it; `null` without a JSON body). An unreachable backend replies
  `{data: null, error: "backend unreachable"}`, never fixture data.
- **Cache.** GETs are cached in memory for 60 s. A successful POST drops the matching GET entry;
  a page read with `fresh: true` skips the cache but still stores the reply.
- **First-visit rule.** An `ingest` message sends one refresh per title per browser session,
  whatever the outcome, and only when the last `page` reply for that title was a `404` from a
  reachable backend; otherwise it replies `{data: null, error: "not ingesting"}` without a
  request. The title goes into an in-memory set before any `await`, then into
  `chrome.storage.session` before the POST, so two tabs asking at once send one POST and a
  restarted worker still finds the title (and replies with the stored outcome). A `refresh`
  message always sends one POST.

## Tests

| Test | Covers |
| --- | --- |
| `tests/test_extension.py` | The manifest, the fixtures' contract keys, no HTML parsing or code evaluation in any script, `node --check` on each script |
| `tests/test_extension_worker.py` | `background.js` against a stubbed Chrome API, `chrome.storage.session` included (`tests/extension_harness.js`) |
| `tests/test_extension_content.py` | The content scripts against a fake DOM (`tests/content_harness.js`) |

These tests need node on the PATH. Without it they fail; they do not skip.
