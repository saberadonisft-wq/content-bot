# Content Bot

Local-first game social-listening workbench. It discovers public game content by keyword, records engagement snapshots, and ranks relevant or rising posts.

## Current capabilities

- FastAPI API with MongoDB Atlas storage, keyword schedules, run tracking, relevance and trend scoring.
- YouTube connector through the official Data API when `YOUTUBE_API_KEY` is configured.
- Source registry for web, Steam, Bluesky, Reddit, X, MediaCrawler platforms, TikTok, Facebook, and Instagram; connectors that need credentials or additional setup report their state instead of failing a run.
- React workbench dashboard with keyword management, source health, live run status, filtering, and CSV/JSON export.

## Run locally

On Windows with Python 3.11+ and Node.js 20+, open PowerShell in this repository and run:

```powershell
.\scripts\launcher.ps1
```

Running `./scripts/launcher.ps1` directly installs missing local dependencies, starts the API in the background, and starts the dashboard at `http://127.0.0.1:5173` without showing a menu. Game news and Steam reviews work without a key. Copy `backend/.env.example` to `backend/.env` only when you want to add options such as `YOUTUBE_API_KEY`.

## V1 test configuration

Your local configuration file is [`backend/.env`](backend/.env). It is ignored by Git and is read explicitly by the API, so it works whether you start from the repository root or from `backend/`. Do not paste its values into chat. Game News, Steam reviews and Bluesky need no key. Reddit uses `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`; X uses `X_BEARER_TOKEN` with Recent Search access. `WEB_FEED_URLS` is an optional public RSS override. MediaCrawler sources use a visible QR/phone login and a local browser profile—do not add social passwords or cookies to `.env`.

If a scan or setup step is unclear, inspect `data/logs/api-stdout.log` and `data/logs/api-stderr.log`. The API also exposes `/api/v1/health` and `/api/v1/ready`; credential values are never written to those logs.

When the dashboard is no longer needed, close the frontend terminal. The API PID is recorded in `data/content-bot-api.json`; stopping it does not modify MongoDB data.

MongoDB Atlas is the active application database. Set `MONGODB_URI` and optionally `MONGODB_DATABASE` (default `content_bot`) in `backend/.env`. The API verifies the connection and creates its required indexes during startup.

To import a legacy SQLite database into MongoDB, stop the API and run `backend/.venv/Scripts/python.exe backend/scripts/migrate_sqlite_to_mongodb.py`. The migration preserves existing IDs, creates indexes, and is idempotent; add `--replace` only when MongoDB documents with matching IDs should be overwritten from SQLite.

The launcher starts the dashboard only. To scan one connector, select a tracked game in the dashboard, open **Sources**, and use **Scan source** or **Login & scan**. Login-gated sources open a visible Cốc Cốc window; the dashboard reports phase, progress, fetched/stored counts and cancellation state.

For maintenance, stop the API before restore or apply-prune operations:

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

The API binds to `127.0.0.1` by default. Browser profiles and raw crawl data remain under `data/` and are intentionally ignored by Git. Keep the Atlas connection string only in the ignored `backend/.env` file.

## Source setup

| Source | Activation | Scope |
| --- | --- | --- |
| YouTube | Set `YOUTUBE_API_KEY` | Official video search and public engagement counts. |
| Xiaohongshu, Douyin, Kuaishou, Bilibili, Weibo, Tieba, Zhihu | MediaCrawler runtime and Cốc Cốc must be installed | Direct submodule integration; a visible browser opens for QR login when required. |
| Game news | Ready by default | Public keyword RSS feed; override with `WEB_FEED_URLS` when desired. |
| Steam reviews | Ready by default | Searches matching games and ingests recent public reviews without retaining Steam account IDs. |
| Bluesky | Ready by default | Official public keyword search with engagement counts; no account or key required. |
| Reddit | Set `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET` | Official OAuth submission search with score and comment counts. |
| X | Set `X_BEARER_TOKEN` | Official Recent Search with public interaction metrics. |
| TikTok / Facebook / Instagram | Approved API or a future user-visible browser flow | API access is preferred; no credential scraping. |

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
