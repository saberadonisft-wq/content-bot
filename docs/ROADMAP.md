# Content Bot roadmap

This roadmap separates code presence from verified collection. A source is only called **verified** after a real batch reaches `succeeded` and stores items through the public API used by the dashboard.

## Current acceptance matrix

| Capability | State | Evidence / remaining gate |
| --- | --- | --- |
| Keyword tracking, aliases, exclusions and per-source caps | Implemented | FastAPI lifecycle tests and dashboard form. |
| Manual and scheduled batches | Live verified | A real scheduled batch ran Game News, skipped login-gated Bilibili with an actionable reason, and opened no MediaCrawler process. Cadence remains anchored across downtime. |
| Game News keyword search | Live verified | Google News RSS batch stored public results. |
| Steam review search | Live verified | `Hades II` batch fetched and stored 5 reviews. |
| Bluesky post search | Live verified | `Hades II` batch fetched and stored 5 posts with public engagement. |
| YouTube | Implemented, optional setup | Requires `YOUTUBE_API_KEY`; live verification still requires a user key. |
| Xiaohongshu, Douyin, Kuaishou, Bilibili, Weibo, Tieba, Zhihu | Dashboard supervised flow verified, login acceptance pending | Sources exposes one-at-a-time **Login & scan**, live fetched/stored progress and safe cancellation. The bridge launches its own visible persistent Playwright browser; Bilibili process launch/profile persistence/cancellation are verified, while QR login and stored results still need user-supervised acceptance per platform. |
| TikTok, Facebook, Instagram | Not implemented | Keep disabled until an approved API client or explicit visible-browser adapter is available. |
| Relevance and trend scoring | Implemented baseline | Keyword match, recency, engagement percentile and snapshot velocity. |
| Exclusion and false-positive control | Live verified | Boundary-aware Latin matching, CJK-compatible matching, zero-score rejection and legacy cleanup. |
| Alias discovery and rule edits | Live verified | Public connectors query aliases, deduplicate results, rescore edits immediately and clean keyword deletion orphans. |
| Language, sentiment and game topics | Live verified baseline | Explainable local heuristics are shown per item; combined filters and matching CSV export were live verified on stored Black Myth Wukong items without an external AI service. |
| Filtered insight summary | Dashboard live verified | The API and workbench show source/language/sentiment/topic/signal distributions and top rising items from exactly the same filtered sample as the item table; Vietnamese + negative returned 3 items in both views. |
| Cross-source story clusters | Dashboard baseline live verified | Conservative title/link grouping is exposed through the filtered insight API and the Workbench. The UI presents member-level sources, item hosts and match reasons, with accessible loading, empty and retry states. Specific article links and strong cross-origin title evidence qualify; homepages/product links do not. Current stored Wukong and Hades II samples correctly return no unsupported clusters, including repeated generic Steam review titles; a positive live-source story remains to be accepted without relaxing those controls. |
| CSV / JSON export | Implemented | Exports use the same filtered items stored in the active MongoDB backend. |
| MongoDB backup, restore and retention pruning | Implemented | Launcher commands use JSON backup/restore and preview/apply retention pruning; legacy SQLite scripts refuse to run when MongoDB is configured. |
| Delete all local data | Implemented | The launcher requires explicit confirmation and a stopped API, then deletes only the validated workspace data directory (including browser profiles and backups). |

## Next milestones

### 1. Complete login-gated source acceptance

- Run one platform at a time; never launch seven login flows together.
- From the dashboard, select the game, open **Sources**, and click **Login & scan** on exactly one platform. **Run now** intentionally submits public sources only.
- Use the launcher's **Scan one source** action so the acceptance run is isolated, reports progress and cancels on timeout.
- Do not configure Chrome remote debugging or port 9222; Content Bot owns a separate visible profile under `data/browser-profile/<platform>`.
- Persist only the local browser profile selected by the user.
- Verify QR/phone login, keyword results, cap enforcement, cancellation and a second run using the saved session.
- Record platform-specific failure messages such as expired session, challenge page and rate limit.

Done when every enabled MediaCrawler source has a real stored-item batch and a repeatable local setup note.

### 2. Add approved Western-platform adapters

- TikTok: use an approved Research/Display API capability if it supports the requested discovery scope; otherwise keep the source disabled.
- Facebook and Instagram: use Graph API access owned or authorized by the user. Do not promise global keyword search when the granted API only supports owned pages/accounts or hashtag discovery.
- Add credentials only through local environment variables; never store tokens in SQLite or browser-visible responses.
- Add contract tests, rate-limit handling and a live acceptance batch per connector.

Done when the registry truthfully reports the granted scope and each `ready` source has live evidence.

### 3. Improve game intelligence

- Normalize a game entity and aliases (`Hades II`, `Hades 2`, studio and character terms) separately from the display name.
- Improve the verified rule-based language baseline with optional Vietnamese/English grouping and user-correctable labels.
- Extend the explainable sentiment/topic baseline with per-game dictionaries and evaluation sets before adding any optional model-based classifier.
- Verify a positive live-source story without relaxing the implemented conservative cluster controls; refine the displayed evidence only from that acceptance data.
- Add time-window and minimum-engagement controls to the accessible filtered overview; keep exact counts and percentages visible beside every bar.
- Add saved filters for source, language, time window, sentiment and minimum engagement.

Done when a user can answer “what is trending around this game, where, and why?” without manually reading every row.

### 4. Reliability and data controls

- Persist pagination checkpoints for resumable batches.
- Add bounded retries with exponential backoff for 429 and transient 5xx responses.
- Add per-source concurrency/rate budgets and a global run queue.
- Add transactional MongoDB restore when the deployment supports replica-set transactions.
- Remove legacy SQLite maintenance tooling after the MongoDB migration path is no longer needed.
- Add structured logs and a diagnostics download with secrets and account identifiers redacted.

Done when interrupted scans resume safely, failure causes are actionable and local data can be audited or removed.

### 5. Packaging

- Keep the setup doctor current as dependencies change; Python, Node, MediaCrawler, active Chromium launch, ports, optional keys, API health and source states are covered.
- Package the production frontend and API behind one local command; the development launcher already avoids reload workers and duplicate listeners.
- Automate Windows clean-install smoke tests; second launch and scoped shutdown have been manually verified.
- Keep the workspace-scoped API state/stop workflow covered as the launcher evolves.
- Document upgrade steps for the MediaCrawler submodule and its isolated environment.

Done when a clean Windows machine can install, scan a public source and export results from one documented workflow.

## Safety boundary

Collect public content only, respect platform terms and rate limits, and do not bypass challenges or access controls. Comments and media downloads remain disabled for the MediaCrawler bridge. Account identifiers and credentials should be minimized, redacted or omitted wherever the research goal does not require them.
