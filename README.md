# Content Bot

Local-first game social-listening workbench. It discovers public game content by keyword, records engagement snapshots, and ranks relevant or rising posts.

## Current capabilities

- FastAPI API with local SQLite storage, keyword schedules, run tracking, relevance and trend scoring.
- YouTube connector through the official Data API when `YOUTUBE_API_KEY` is configured.
- Source registry for web, Steam, Bluesky, Reddit, X, MediaCrawler platforms, TikTok, Facebook, and Instagram; connectors that need credentials or additional setup report their state instead of failing a run.
- React workbench dashboard with keyword management, source health, live run status, filtering, and CSV/JSON export.

## Run locally

On Windows with Python 3.11+ and Node.js 20+, open the repository in VS Code, run **Tasks: Run Task**, then choose **Content Bot: Start**. VS Code starts both services at the same time in separate foreground terminals:

- **Content Bot: Backend** — FastAPI/Uvicorn at `http://127.0.0.1:8000`.
- **Content Bot: Frontend** — Vite at `http://127.0.0.1:5173`.

Both processes remain attached to their VS Code terminals, so logs and startup failures are visible immediately. Terminate the compound task, or press `Ctrl+C` in each terminal, to stop them. No API process is launched in the background. To run only one service outside VS Code, use `./scripts/launcher.ps1 -Action backend` or `./scripts/launcher.ps1 -Action frontend`; missing local dependencies are installed on first use. Game news and Steam reviews work without a key. Copy `backend/.env.example` to `backend/.env` only when you want to add options such as `YOUTUBE_API_KEY`.

### Desktop development mode

To display signed-in social pages directly inside Live Wall cards, run **Tasks: Run Task → Content Bot: Desktop Dev**, or:

```powershell
.\scripts\launcher.ps1 -Action desktop
```

This is a development shell, not a packaged installer. It reuses Auth Server, backend, and Vite when they are already running; otherwise it starts them. React/CSS changes update through Vite immediately, Python application changes restart the owned service, and changes below `desktop/` restart only Electron. You do not need to package the app between tests. Stop it with `Ctrl+C` in its terminal.

Open **Nguồn & Video → Trực tiếp** in the desktop window. Supported saved channel pages are placed directly in their cards using an isolated per-user browser session. The normal browser version continues to use official embeds and the explicit Cốc Cốc fallback.

## V1 test configuration

Your local configuration file is [`backend/.env`](backend/.env). It is ignored by Git and is read explicitly by the API, so it works whether you start from the repository root or from `backend/`. Do not paste its values into chat. Game News, Steam reviews and Bluesky need no key. Reddit uses `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`; X channel pages always retain the public view-only embed, while `X_BEARER_TOKEN` additionally enables manual official-API ingestion for saved creator timelines and recent search. `WEB_FEED_URLS` is an optional public RSS override. MediaCrawler sources use a visible QR/phone login and a local browser profile—do not add social passwords or cookies to `.env`.

## Gemini subtitle generation

Subtitle Studio sends a video through the official Gemini REST API, asks Gemini to use audio, visible captions and scene context, then imports the returned Vietnamese cues directly into the timeline. The worker uses a Gemini API key and never requires a globally installed CLI.

Configure a key from Google AI Studio in `backend/.env`:

```powershell
# backend/.env
GEMINI_API_KEY=your-google-ai-studio-key
CONTENT_BOT_GEMINI_MODEL=gemini-3.6-flash
```

Restart Content Bot after editing `.env`. Because Gemini accepts at most 20 MB per attached local file, Content Bot automatically creates a temporary MP4 proxy below 19 MB. Videos longer than three minutes are processed in sequential chunks and their timestamps are joined automatically. Temporary proxy files are removed after success, failure or cancellation.

Set `CONTENT_BOT_GEMINI_MODEL` and the timeout/retry fields in `backend/.env` when needed. The old `CONTENT_BOT_GEMINI_CLI_*` names are accepted for one release with a deprecation warning; `CONTENT_BOT_GEMINI_CLI_PATH` has no effect. Quota, service availability and data handling follow the configured Google project/key.

If a scan or setup step is unclear, inspect the **Content Bot: Backend** terminal. The API also exposes `/api/v1/health` and `/api/v1/ready`; credential values are never written to terminal output.

When the dashboard is no longer needed, terminate both VS Code tasks. Stopping either process does not modify local application data.

SQLite is the default application database. Topics, crawler runs, collected content, metrics and comments are stored in `data/content-bot.db`; SQLite WAL mode allows safe local reads and writes without installing or starting MongoDB. The API imports the repository's legacy SQLite tables into the current document schema automatically and idempotently.

The auth-server remains independent and is responsible for users, login and authorization. Its own database or fallback configuration does not gate creation of topics in the main Content Bot API.

API credentials entered in Settings are encrypted in the local Credential Vault. When the Vault is locked, new operations fall back to matching `.env` values. Database paths and connection strings are startup configuration, not Vault credentials.

For a legacy deployment that deliberately keeps application data in MongoDB, set `CONTENT_BOT_STORAGE_BACKEND=mongodb`, `MONGODB_URI`, and optionally `MONGODB_DATABASE` in `backend/.env`, then restart the backend. SQLite remains the recommended desktop/local mode.

To scan one connector, select a tracked game in the dashboard, open **Sources**, and use **Scan source** or **Login & scan**. Login-gated sources open a visible Cốc Cốc window; the dashboard reports phase, progress, fetched/stored counts and cancellation state.

For a local backup, stop the backend and copy `data/content-bot.db` together with any adjacent `content-bot.db-wal` and `content-bot.db-shm` files. MongoDB maintenance scripts below apply only when `CONTENT_BOT_STORAGE_BACKEND=mongodb`:

```powershell
.\scripts\launcher.ps1 -Action setup-mediacrawler
backend\.venv\Scripts\python.exe backend\scripts\backup_mongodb.py --destination data\backups\content-bot.json
backend\.venv\Scripts\python.exe backend\scripts\restore_mongodb.py --source data\backups\content-bot.json --replace-current
backend\.venv\Scripts\python.exe backend\scripts\prune_mongodb.py --days 90
backend\.venv\Scripts\python.exe backend\scripts\prune_mongodb.py --days 90 --apply
```

For MediaCrawler sources, Content Bot starts its own visible Cốc Cốc browser; you do **not** need to launch Chrome with remote debugging or configure port `9222`. Login state is persisted per platform under `data/browser-profile/<platform>`. Keep the Cốc Cốc window open, scan the QR code with the platform's mobile app, complete any phone/slider confirmation, and let the command finish. The QR code is rendered by the platform in that visible browser; Content Bot does not download or proxy it to the dashboard. A timeout closes the adapter and its managed browser tree and records the batch as `cancelled`.

The dashboard exposes the same supervised flow. Select a tracked game in **Workbench**, open **Sources**, then use **Scan source** for a public connector or **Login & scan** for one MediaCrawler connector. Login-gated sources are always started one at a time and never run from the background **Run now** action. The live batch panel shows a determinate progress bar for public scans, an indeterminate startup/login bar for Cốc Cốc, the current phase, and fetched/stored counts. **Cancel run** closes the managed crawler browser tree safely.

Scheduled runs are unattended and therefore only execute sources that do not require login. A selected login-gated source is recorded as `skipped` with an explanation; it never opens a browser or QR prompt in the background. Use **Run now** when you are present to complete login. Schedule times stay anchored to the configured cadence after an API restart or downtime instead of drifting from the restart time.

The API binds to `127.0.0.1` by default. The SQLite database, browser profiles and raw crawl data remain under `data/` and are intentionally ignored by Git. Keep any optional remote database connection string only in the ignored `backend/.env` file.

## Source setup

| Source | Activation | Scope |
| --- | --- | --- |
| YouTube | Set `YOUTUBE_API_KEY` | Official newest-first search and saved-channel uploads with frontier-safe checkpoints, explicit per-run quota budgets, and optional deep health via `/api/v1/sources?deep=true`. |
| Xiaohongshu, Douyin, Kuaishou, Bilibili, Weibo, Tieba, Zhihu | MediaCrawler runtime and Cốc Cốc must be installed | Direct submodule integration; a visible browser opens for QR login when required. |
| Game news | Ready by default | Validated HTTPS RSS/Atom discovery with conditional requests and feed-level warnings; override with a JSON-array `WEB_FEED_URLS` allowlist when desired. |
| Steam reviews | Ready by default | Exact-title public discovery with ambiguity warnings, or pin a game by saving its `/app/<id>` URL; newest-first pagination never retains Steam account IDs. |
| Bluesky | Ready by default | Public AppView keyword/author discovery plus manual bounded public reply-tree scans for stored posts; stable AT-URI-derived identities, HMAC authors, best effort, no account or key required. |
| Mastodon | Ready by default | Hashtag/saved-account discovery plus manual bounded status-context reply scans across the exact `MASTODON_INSTANCES` allowlist. Not Fediverse-wide full-text search. |
| Reddit | Set `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` | Official OAuth keyword and saved subreddit/user scans plus manual bounded comment-tree scans for stored posts; application token is cached in memory and authors are pseudonymized. |
| X | Public view-only embed; optional `X_BEARER_TOKEN` for official recent search and saved creator timelines | Embed remains independent. API ingestion is read-only, pay/access-gated, checkpointed per query/account, and manual by default. |
| TikTok | Approved Login Kit + Display API app; set client key/secret and the exact static HTTPS callback | Dashboard OAuth requests `user.info.basic`, `user.info.profile`, and `video.list`, verifies the consenting creator handle, stores tokens in the encrypted local vault, and reads only that account's public videos. No global search or browser scraping. |
| Instagram | Set pinned `META_GRAPH_API_VERSION`, approved `META_ACCESS_TOKEN`, and `INSTAGRAM_PROFESSIONAL_USER_ID` | Official best-effort hashtag discovery with a persistent rolling unique-hashtag budget; no browser fallback. |
| Facebook | Set pinned `META_GRAPH_API_VERSION`, approved `FACEBOOK_PAGE_ACCESS_TOKEN`, `FACEBOOK_PAGE_ID`, and `FACEBOOK_PAGE_USERNAME` | Official bounded feed scan for that one explicitly authorized Page through a saved channel. No global search, profiles, Groups, arbitrary public Pages, or browser/cookie fallback. |

For TikTok Web Login Kit, register `https://<public-api-origin>/api/v1/auth/tiktok/callback` in the TikTok developer portal, set the same URL as `TIKTOK_REDIRECT_URI`, and access Content Bot's OAuth start route through that same HTTPS origin. The dashboard returns only a connection outcome; authorization codes and tokens remain server-side. `CONTENT_BOT_FRONTEND_URL` selects the dashboard destination after callback.

Crawler observations expire after `CONTENT_BOT_CRAWLER_RETENTION_DAYS` (default 90) and are cleaned on the configured interval. Deleting one source's crawler data requires an exact confirmation through `POST /api/v1/crawler-data/{source_id}/delete`; application-owned browser profiles are excluded from automatic retention and require the stronger `DELETE <source_id> DATA AND PROFILES` confirmation while no crawler holds the profile lock.

Before cutting an individual provider operation over, operators can run the
manual aggregate-only canary documented in
[`docs/crawler-provenance/LIVE_CANARY.md`](docs/crawler-provenance/LIVE_CANARY.md).
It requires `--live`, accepts the query/target only on stdin, enforces tiny
budgets, never writes production crawler data and never includes raw content or
credentials in its report.

The read-only Phase 11 gate is documented in
[`docs/crawler-provenance/CUTOVER_AUDIT.md`](docs/crawler-provenance/CUTOVER_AUDIT.md).
It reports per-source provider/canary/provenance blockers and legacy runtime
artifacts; it never removes them automatically.

Clean-room browser search for XHS, Douyin, Kuaishou, Weibo and Zhihu requires a
separately observed and reviewed DOM artifact; no selectors are bundled or
copied from MediaCrawler. See
[`docs/crawler-provenance/OBSERVED_DOM_CONTRACTS.md`](docs/crawler-provenance/OBSERVED_DOM_CONTRACTS.md).
Missing/invalid contracts remain `setup_required`, and switching providers
requires an explicit per-source operation override.

The MediaCrawler repository is included as a Git submodule at `vendor/mediacrawler`. Its isolated runtime must be installed before using login-gated sources.

Restart Content Bot, create or edit a tracked game, select one of the ready MediaCrawler sources, and click **Run now**. Complete QR or phone verification in the visible Cốc Cốc window if requested. The scan waits up to ten minutes for login and results, collects at most the configured per-source cap, and deliberately disables comment and media downloads.

To scan one platform, select `Black Myth Wukong` in the dashboard and use **Login & scan** on `bilibili`.

Valid MediaCrawler source IDs are `xhs`, `douyin`, `kuaishou`, `bilibili`, `weibo`, `tieba`, and `zhihu`. If a previous run was interrupted by an API restart, startup closes its stale database rows so it cannot block the next scan.

`MEDIACRAWLER_COMMAND` is only an advanced override. Normal use invokes the bundled adapter at `backend/scripts/mediacrawler_adapter.py`, which calls MediaCrawler directly and converts its platform-specific JSONL into Content Bot items.

This boundary is deliberate: MediaCrawler has its own non-commercial learning-use terms. Use it only within those terms, respect each platform's rules and rate limits, and do not collect private content, comments, media, or bypass access controls.

The verified source matrix and remaining implementation milestones are tracked in [`docs/ROADMAP.md`](docs/ROADMAP.md).

Keyword matching is case- and accent-insensitive. Latin/Vietnamese aliases use word or phrase boundaries to avoid substring leaks such as `NTE` matching `content`; unsegmented CJK terms retain substring matching. Excluded or otherwise zero-relevance results count as fetched but are not stored for the keyword, shown in trends, or included in exports. Startup removes legacy zero-score matches while preserving content referenced by another valid keyword.

Each listed item also gets local, explainable `rule-based-v1` insights: a conservative language hint, positive/negative/mixed/neutral sentiment, and game topics such as gameplay, performance, bugs, updates, monetization, story and community. The dashboard shows the matched signal words and can combine source, language, sentiment and topic filters. CSV/JSON exports accept the same filters and include the same analysis. These labels are discovery aids rather than factual or linguistic ground truth; neutral/unknown is retained when the text has no supported signal.

`GET /api/v1/insights/summary?keyword_id=<id>` aggregates the same filtered sample into source, language, sentiment, topic and signal buckets plus the highest-trend items. The workbench renders those exact buckets as a text-labelled overview above the item table and refreshes it with the same active filters. It accepts the item filters `source_id`, `query`, `language`, `sentiment`, `topic`, and `min_relevance`; `top_limit` controls the returned rising-item list. Topic percentages are intentionally multi-label and can overlap. The response and dashboard retain the method and caveat so heuristic output is not presented as ground truth.

`GET /api/v1/insights/clusters?keyword_id=<id>` provides a conservative cross-source story baseline over that same filtered sample. A group requires either the same normalized **specific article link** or strong title-term overlap across distinct origins; homepages and product/store links do not form a cluster by themselves. Title similarity alone never groups two items from the same source. The game name and aliases are removed from comparison so generic results such as repeated Steam recommendation titles do not become false trends. Use `min_items` and `limit` to bound the response; the response distinguishes the total qualifying clusters from the returned top slice. Every cluster includes its members, item hosts, sources and match reasons, and should still be reviewed before being treated as one story. The Workbench renders the same filtered clusters as expandable rows with their member links, item hosts and grouping evidence; it explicitly explains when the evidence is insufficient rather than inventing a trend.

The game name and every Include term are crawl queries, not scoring-only hints. YouTube combines them as an OR query; Game News, Steam and Bluesky allocate the per-source cap across aliases and deduplicate repeated results. Editing aliases or exclusions immediately rescores stored matches. Deleting a tracked keyword removes its batches and orphaned content while retaining items referenced by another keyword.

For a no-login smoke test, create the keyword `Hades II`, select **Bluesky**, **Steam reviews**, or **Game news**, set the per-source maximum to `10`, and click **Run now**. Results and the last three runs appear in the workbench; CSV and JSON exports use the same stored items.
