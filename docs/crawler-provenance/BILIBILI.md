# Bilibili provider provenance

Status: Phase 11 cutover provider, 2026-08-13. Public DOM keyword search has two separated passing canaries and is the configured active provider. Bounded manual comment-tree ingestion is implemented but remains a separate explicit rollout.

## Official evidence reviewed

- [Bilibili Open Platform documentation home](https://openhome.bilibili.com/doc): describes account authorization, public user information, video management, data for authorized users/videos, webhooks and sandbox support.
- [Bilibili Open Platform](https://open.bilibili.com/platform): requires identity verification, application creation and authorization before developer capabilities are available.
- [Bilibili Open Platform developer agreement](https://open.bilibili.com/agreement/developer-service): scopes the service and user/operation data around the developer application and associated/authorized UP accounts.
- [Playwright Python BrowserType documentation](https://playwright.dev/python/docs/api/class-browsertype): documents a separate persistent user-data directory and warns against automating the user's default Chrome profile. CBCE uses the optional Apache-2.0 `playwright==1.61.0` package directly, not MediaCrawler's browser wrapper.

## Resulting implementation boundary

- `bilibili_open_platform` is a planned, approval-gated provider for associated/authorized UP accounts. It does not claim global public keyword search.
- `cbce_bilibili` now has experimental clean-room contracts for bounded public DOM keyword search, video detail, creator video listing, root comments, child comments and media metadata. Keyword search and manual comment-tree ingestion have implemented handlers; both remain disabled by default until their operation-specific rollout gates pass. Other operations remain gated until their evidence is complete.
- The current legacy bridge remains present for the declared rollback window, but configured Bilibili search resolves to `cbce_bilibili` rather than the bridge.
- The target parser recognizes/canonicalizes public Bilibili URLs and IDs. The DOM provider contains only selectors independently observed below; it contains no internal endpoint, request signature, copied response fixture or MediaCrawler implementation.

## Project-owned public DOM observation — 2026-08-13

A temporary, logged-out CBCE profile opened `https://search.bilibili.com/all` with a neutral test keyword. Only tag names, class names, attribute names, host/path and query names were printed; content values, UID, BV/av values, cookies and headers were not retained.

Observed behavior used by the experimental DOM provider:

- Search accepts `keyword` and `page` query parameters; direct `page=2` selected page 2. The first page hydrated only when the redundant `page=1` parameter was omitted in two isolated comparison runs, so the provider deliberately emits only `keyword` for page 1.
- Result hierarchy: `.video-list-item .bili-video-card`, title node `.bili-video-card__info--tit`, stats under `.bili-video-card__stats--item`, pagination side buttons `.vui_pagenation--btn-side`.
- Page 1 yielded 42 direct video cards after hydration in the observation run. The checkpoint therefore includes an offset within the page as well as term and page number.
- No internal JSON endpoint, signing behavior, request header, response payload or MediaCrawler artifact was used.

This observation permits an experimental public DOM search provider behind the CBCE feature flag. It does not permit login automation, comments, creator crawling, media download or an assertion of complete search coverage.

### Video detail and media metadata

A separate temporary profile observed one public `/video/{id}` page. The provider uses only independently observed DOM/standard metadata: `h1.video-title`, `meta[name='description']`, `meta[property='video:release_date']`, `.view-text`, `.video-like-info`, `.video-fav-info`, `meta[property='og:image']` and `meta[property='video:duration']`. It retains a validated HTTPS cover URL and bounded duration only. It deliberately does not persist a temporary player URL or download the video.

### Creator video listing

A separate temporary profile observed `/upload/video` under a public creator space. The provider reads `.video-list .upload-video-card`, `.bili-video-card__title`, public `/video/` links and visible VUI pagination controls. Its `page/offset` checkpoint preserves exact limits. No creator profile, follower graph, nickname or raw creator identity is persisted.

### Root and child comments

The public page exposes an open `bili-comments` Shadow DOM. The project independently observed `bili-comment-thread-renderer`, `bili-comment-replies-renderer`, `bili-comment-reply-renderer` and page-owned component `data` fields needed for comment identity, hierarchy, timestamp, like count and text. Root and child pagination have separate cursor codecs and separate budgets.

In a logged-out canary the component declared substantially more comments than it rendered and displayed `#limit-mask`. Clicking the visible child `#view-more bili-text-button` did not expand the partial list. The provider therefore returns typed `AUTH_REQUIRED`; it never treats the preview as complete and never calls an internal comment endpoint. A user-authenticated application-owned `default` profile is required for full comment coverage. CAPTCHA/challenges remain manual.

## Validation evidence — 2026-08-13

- A direct logged-out Cốc Cốc canary fetched three public search records through the DOM provider with an exact item limit, an offset-bearing checkpoint and the `view_count` metric only. Records stayed in memory.
- The same three-item canary passed through the isolated `cbce.worker.v1` subprocess path. Stdout contained protocol events only, stderr was empty, and the owned browser/process tree closed after completion.
- The opt-in live test `CBCE_LIVE_BILIBILI=1` passed locally. It remains skipped in the default suite because it uses the network and opens a visible browser.
- A second bounded direct canary at a later checkpoint again returned exactly three canonical public search records and successfully fetched detail for the first record. A fresh isolated-worker canary also returned `3/3` canonical records with `persisted=false`; both used temporary application-owned profiles that were removed after the run.
- Search, detail, creator and comment operations share one per-platform/account `default` profile in the isolated worker. Creator/comments are now wired through the same bounded worker lifecycle as search/detail. Root/child/total budgets remain distinct.
- The connector now exposes internal root and child scan results as canonical `CommentRecord` tuples, revalidates identity/hierarchy/timestamp/provenance, propagates typed worker errors, and can feed the shared comment persistence boundary after rollout. Root and child operations remain distinct so a preview cannot silently satisfy full-tree coverage.
- The focused Bilibili and worker contract suite passed 76 tests after adding detail, creator, comments and media metadata.
- After registry wiring was completed on 2026-08-13, the generic aggregate-only cutover runner selected `cbce_bilibili` through an explicit provider override and passed a fresh isolated-worker canary with `2/2` unique records, exact item/request/deadline budgets and `persisted=false`.
- A privacy-preserving shadow runner now reduces both providers immediately to keyed identity digests, counts, timestamp bounds and field/metric completeness. It never writes shadow items to Content Bot persistence; legacy scratch is isolated under an owned UUID directory and removed in `finally`.
- The first bounded shadow attempt could not produce a comparison because the legacy bridge remained in its interactive login/startup path. Later 15-second and 30-second retries both returned the typed result `BASELINE_TIMEOUT`, then automatically closed their whole process trees and removed their scratch directories. The latest cleanup audit found zero shadow scratch children and no remaining Python/Chrome/Cốc Cốc process from the run.

The search minimum-operation cutover gate now passes: two retained aggregate-only reports completed 3663 seconds apart, each returned 2/2 unique records without persistence, and the configured active provider resolves to `cbce_bilibili`. Aggregate overlap could not be measured because the legacy baseline never emitted within its bounded deadline; this remains a documented evidence limitation rather than permission to claim full parity. Full comment coverage still needs a logged-in application-profile canary, and the bridge remains only as rollback during the window.

The persistence-ready connector does not change that gate: `cbce_bilibili/list_comments` is now an implemented, partial, manual-only manifest operation and is exposed through the shared item-comment API only when CBCE browser/profile preflight passes. The API fetches stable root records first, then bounded child pages per root with one shared total/request budget; it validates provider/content/parent/root identity before idempotent Mongo persistence. Missing login or an unready rollout returns 503/typed auth failure and writes nothing. Independent child-only UI exposure remains unnecessary because the tree scan owns that bounded expansion.

Rollout is explicit per operation. A manual comment-tree trial requires both the source connector and comment operation to select CBCE, for example `{"bilibili":{"search":"cbce_bilibili","list_comments":"cbce_bilibili"}}`; selecting only one does not make the other ready.

## Prohibited sources and artifacts

Do not copy MediaCrawler WBI code, endpoint wrappers, login selectors, browser scripts, payload/header constants, normalizers or fixtures. Any future selector, endpoint, response field or third-party dependency needs a new provenance entry identifying official documentation or a sanitized observation captured by this project.
