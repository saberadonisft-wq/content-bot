# Content Bot

Local-first game social-listening workbench. It discovers public game content by keyword, records engagement snapshots, and ranks relevant or rising posts.

## Current capabilities

- FastAPI API with local SQLite storage, keyword schedules, run tracking, relevance and trend scoring.
- YouTube connector through the official Data API when `YOUTUBE_API_KEY` is configured.
- Source registry for web, Steam, Bluesky, Reddit, X, MediaCrawler platforms, TikTok, Facebook, and Instagram; connectors that need credentials or additional setup report their state instead of failing a run.
- React workbench dashboard with keyword management, source health, live run status, filtering, and CSV/JSON export.

## Run locally

On Windows with Python 3.11+ and Node.js 20+, open the repository in VS Code, run **Tasks: Run Task**, then choose **Content Bot: Start**. This launches the desktop development app: it starts or reuses Auth Server, backend and frontend, then opens the Electron window. Stop it with `Ctrl+C` in its task terminal or close the app window. Services that were already running remain available.

For browser development, choose **Content Bot: Web Services**, then open `http://127.0.0.1:5173`. This task starts the three services in separate foreground terminals:

- **Content Bot: Backend** — FastAPI/Uvicorn at `http://127.0.0.1:8000`.
- **Content Bot: Frontend** — Vite at `http://127.0.0.1:5173`.
- **Content Bot: Auth Server** — authentication at `http://127.0.0.1:8080`.

The web service processes remain attached to their VS Code terminals, so logs and startup failures are visible immediately. Terminate the compound task, or press `Ctrl+C` in each terminal, to stop them. To run only one service outside VS Code, use `./scripts/launcher.ps1 -Action backend` or `./scripts/launcher.ps1 -Action frontend`; missing local dependencies are installed on first use. Game news and Steam reviews work without a key. Copy `backend/.env.example` to `backend/.env` only when you want to add options such as `YOUTUBE_API_KEY`.

### Desktop development mode

**Content Bot: Start** uses **Content Bot: Desktop Dev**, which supports signed-in social pages directly inside Live Wall cards. You can also launch it with:

```powershell
.\scripts\launcher.ps1 -Action desktop
```

This is a development shell, not a packaged installer. It reuses Auth Server, backend, and Vite when they are already running; otherwise it starts them. React/CSS changes update through Vite immediately, Python application changes restart the owned service, and changes below `desktop/` restart only Electron. You do not need to package the app between tests. Stop it with `Ctrl+C` in its terminal.

Open **Nguồn & Video → Trực tiếp** in the desktop window. Supported saved channel pages are placed directly in their cards using an isolated per-user browser session. The normal browser version continues to use official embeds and the explicit Cốc Cốc fallback.

## V1 test configuration

Your local configuration file is [`backend/.env`](backend/.env). It is ignored by Git and is read explicitly by the API, so it works whether you start from the repository root or from `backend/`. Do not paste its values into chat. Game News, Steam reviews and Bluesky need no key. Reddit uses `REDDIT_CLIENT_ID` and `REDDIT_CLIENT_SECRET`; X channel pages always retain the public view-only embed, while `X_BEARER_TOKEN` additionally enables manual official-API ingestion for saved creator timelines and recent search. `WEB_FEED_URLS` is an optional public RSS override. MediaCrawler sources use a visible QR/phone login and a local browser profile—do not add social passwords or cookies to `.env`.

## Gemini subtitle generation

Subtitle Studio sends video through the Gemini REST API to produce Vietnamese subtitles using speech, visible text and scene context. Source text and language are kept separately for review and optional audio alignment. Video without audio can still be processed for visible text.

Generation prompt version 11 allows a sentence or a clause per cue; a complete sentence is not required. Splitting at commas is allowed when it preserves the original subtitles’ content, context, order and timing. Do not merge original cues just to complete a sentence or split mechanically at every comma. When original subtitles are visible, Gemini must translate their content in context and follow their appearance/disappearance times; audio helps interpret the text, with discrepancies flagged for review. Blocks containing multiple sentences must be split using observed boundaries within the original block. The prompt also requires checking all overlaps and uncovered gaps before returning JSON, preserving valid silence and avoiding evenly divided or invented timestamps. The copied JSON prompt follows the same rules. Prompt changes invalidate generation caches and chunk checkpoints, so a new generation uses the updated instructions. Context comes from neighboring original subtitles and audio; users do not need to supply character names or relationships. Optional notes can supplement the translation, but no character profiling stage is required. These are model instructions, not a guarantee of subtitle accuracy.

Configure a key from Google AI Studio in `backend/.env`:

```powershell
# backend/.env
GEMINI_API_KEY=your-google-ai-studio-key
CONTENT_BOT_GEMINI_MODEL=gemini-3.6-flash
```

Restart Content Bot after editing `.env`. In **Settings → Gemini AI**, unlock the Credential Vault to import multiple keys (one per line), name them and enable/disable them. Generation uses all enabled keys automatically, with one active segment per key and fair rotation between available keys. Project groups are no longer used or required. Once the key list has been saved, an empty, disabled or locked list does not fall back to the old environment key.

The pinned Silero VAD finds pauses near a two-minute target. A segment can extend past the target to avoid cutting speech; a resource limit produces an explicit error if no suitable pause is found. Segments include neighboring context and keep timestamps on the original video timeline. The application creates proxies below 19 MiB as its own resource policy and uploads them through the Files API. Twelve enabled keys allow twelve simultaneous segments when enough segments and eligible keys are available; proxy compression remains limited to two processes. Worker count is set from enabled keys at run start, while the dispatcher checks key availability before assigning each segment. Temporary proxies are cleaned up; completed segment checkpoints can be reused after a failure or cancellation with **Tiếp tục các đoạn còn thiếu**. Cache retention defaults to seven days and 2 GiB.

Generation HTTP 503 immediately moves down the supported `gemini-3.8-flash` → `gemini-3.7-flash` → `gemini-3.6-flash` chain, starting at the selected model. Model availability and permissions still depend on the key. File API errors do not switch models. Quota responses pause the affected key/model according to retry instructions within a bounded deadline; other keys remain eligible until they receive their own errors. The default remains `gemini-3.6-flash`; unrelated model families are never silently substituted.

If chunks remain incomplete after temporary errors, generation automatically performs up to three recovery rounds in the same job. Only incomplete eligible chunks are retried; completed checkpoints are preserved. With the default retry settings, recovery waits 5, 10 and 20 seconds and displays a countdown; the configured retry base can lengthen these waits, capped at 60 seconds. Overloaded models become eligible again after the wait, while unsupported models, invalid keys, denied permissions and daily quota restrictions remain blocked. Cancel also interrupts the recovery wait. Each recovery round has bounded per-chunk request/deadline budgets; setting `CONTENT_BOT_GEMINI_MAX_RETRIES=0` disables recovery. **Tiếp tục các đoạn còn thiếu** remains available after persistent errors exhaust recovery or after cancellation.

**Tạo lại phụ đề** generates a new version and saves the existing document. Open **Phiên bản phụ đề** to compare and explicitly select the version to use. **Căn lại thời gian** supports all cues, flagged cues, the selected cue or a time range. It uses original-language text, respects locks and retains uncertain timing. Additional ASR alignment is optional and requires a locally available model; millisecond precision does not imply equivalent timing accuracy.

For local ASR, install `backend` with the `alignment` extra for CPU or `alignment-gpu` for Faster-Whisper CUDA inference. The GPU extra pins the cuBLAS/cuNVRTC wheels used by the Windows worker; the worker registers their DLL directories before loading CTranslate2 and records peak worker RSS plus sampled GPU memory in benchmark artifacts. The benchmark still needs corpus and natural-video validation before its WER/CER is treated as a quality guarantee.

**Kiểm tra sửa chữa bằng AI** runs one combined review when you click **Sửa bằng AI**. Select the whole video, a time range or a group of cues. Every reviewed clip is checked for content errors, overly long subtitles and abnormal timing together, using the video, original text, translation and ten seconds of surrounding media context. Before sending media, the local scan flags cues containing two or more sentences, including short cues, and supplies sentence counts plus exact intervals for every overlap and uncovered gap. Gaps include the beginning/end of the video and the entire middle of long empty intervals; silence is a candidate for inspection, not proof of missing speech. No separate scan or review tabs are needed. Enabled API keys process clips in parallel, with checkpoint resume.

Gemini proposes content corrections, shorter cues and timing changes in one result list. A cue that is both long and mistimed can be split and repositioned in the same proposal. Each cue appears in at most one proposal per clip, and edits stay within the selected scope. Pure timing changes preserve wording; readability splits conserve translated/source words and produce cues of at most one sentence, 84 characters, six seconds and two lines. Locked cues are context only. Inspect before/after text, timestamps and evidence, then apply individual proposals or all valid proposals, skip, or undo. New edits to affected cues or context cause conflicts instead of being overwritten. Sentence counts use punctuation heuristics with exceptions for decimals, common abbreviations and ellipses; results still need human review. Existing review records remain available; newly started combined reviews use prompt version 5.

Invalid proposals are rejected individually with warnings, preserving valid suggestions without repeating the entire Gemini request. Mixed groups containing unauthorized cues are rejected as a whole. Progress shows completed/active/failed clips, elapsed time and the current upload, analysis or retry step. Partial results and warnings remain visible after a job failure. Resume retries only unfinished clips. Three consecutive clip failures stop new work while already running clips finish saving; combined review allows up to 120 seconds per generation response within a 300-second clip budget.

Advanced generation settings expose pause/segment targets, optional alignment and shared terminology. Concurrency follows enabled key count; legacy `max_concurrent`, `CONTENT_BOT_GEMINI_MAX_CONCURRENT` and `CONTENT_BOT_GEMINI_GROUP_CONCURRENT` values are accepted but ignored. Per-segment progress includes key names, actual models and wait/error reasons. Timeout, retry and cache defaults are documented in `backend/.env.example`. Legacy `CONTENT_BOT_GEMINI_CLI_*` configuration remains accepted with a deprecation warning; `CONTENT_BOT_GEMINI_CLI_PATH` has no effect.

See the [Gemini pipeline handoff and acceptance checklist](docs/GEMINI_SUBTITLE_PIPELINE_ACCEPTANCE.md) for verified behavior, sample artifacts and the pending real API/multiple-key/video acceptance steps.

If a scan or setup step is unclear, inspect the **Content Bot: Backend** terminal. The API also exposes `/api/v1/health` and `/api/v1/ready`; credential values are never written to terminal output.

When the dashboard is no longer needed, terminate both VS Code tasks. Stopping either process does not modify local application data.

SQLite is the default application database. Topics, crawler runs, collected content, metrics and comments are stored in `data/content-bot.db`; SQLite WAL mode allows safe local reads and writes without installing or starting MongoDB. The API imports the repository's legacy SQLite tables into the current document schema automatically and idempotently.

The auth-server remains independent and is responsible for users, login and authorization. Its own database or fallback configuration does not gate creation of topics in the main Content Bot API.

API credentials entered in Settings are encrypted in the local Credential Vault. Legacy credentials may fall back to matching `.env` values while locked; a saved Gemini key list is authoritative and disables that fallback. Database paths and connection strings are startup configuration, not Vault credentials.

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


.\scripts\launcher.ps1 -Action desktop

## Refactoring verification

Local voice generation runs one model worker at a time, including across backend restarts. Pausing keeps completed WAV/checkpoint pairs; continuing recovers valid audio from interrupted runs before generating missing clips. On Windows, cancellation stops the entire worker process tree. A supervised worker exits within approximately 30 seconds of losing its backend heartbeat; standalone Kaggle packages do not require this heartbeat.

Voice workers use pure ONNX on CPU (no PyTorch import), with one inter-op thread and ONNX idle spinning disabled. GPU v3 workers support small batches while keeping a separate WAV/checkpoint for every subtitle; a failed batch is split and the smaller batch limit is retained for that run. CPU and v2 stay sequential. Batch sampling can produce different waveforms, so compare pronunciation and missing/repeated words when evaluating quality.

Machine-specific settings are read from the ignored `runtimes/voiceover/performance-profile.json` when its SDK/model revision matches. Without a profile, defaults are four CPU threads, two GPU-support CPU threads, and GPU batches of two. `CONTENT_BOT_VOICE_THREADS` (1–8) and `CONTENT_BOT_VOICE_BATCH_SIZE` (1–4) override these settings for the worker process. `CONTENT_BOT_VOICE_PROFILE` can point to another profile. These settings do not change the model, sample rate, temperature, repetition penalty, or subtitle boundaries. See [measured balance on the i5-13420H / RTX 4050](docs/VOICEOVER_HARDWARE_BALANCE.md) for the selected local settings and measurement limits.

Module ownership and local verification commands are documented in [Module boundaries](docs/MODULE_BOUNDARIES.md). The [refactoring acceptance report](docs/REFACTOR_ACCEPTANCE.md) tracks measured results and remaining checks against the [original review](docs/REFACTOR_REVIEW_2026-09-12.md).

SQLite query changes are additive. Set `CONTENT_BOT_INDEXED_ITEM_QUERIES=false` to use the fallback read path. Before migrating real data, create a consistent backup with `backend/scripts/backup_sqlite.py SOURCE DESTINATION`; it uses SQLite online backup, checks integrity, and refuses to overwrite the destination.
