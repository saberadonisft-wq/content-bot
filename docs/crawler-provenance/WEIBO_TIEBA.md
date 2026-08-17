# Weibo and Tieba provider provenance

Status: Phase 11 partial cutover, 2026-08-13. Tieba keyword search has a project-observed DOM contract, an isolated worker, two separated successful bounded canaries and is the configured active provider. Weibo still awaits interactive authentication.

## MediaCrawler behavioral specification retained

- Both sources follow the browser-session lifecycle: application-owned persistent profile → visible/manual login when required → bounded search/detail/creator/comments → normalization → Content Bot persistence.
- Weibo needs domain-aware PC/mobile session handling and offers search/detail/creator/root comments with only partial embedded child-comment coverage in the baseline.
- Tieba separates keyword, forum, detail and creator targets; root/child budgets must be bounded independently.

These are behavioral requirements only. No MediaCrawler endpoint wrapper, signature, selector, payload, header, anti-detection script, fixture or normalizer is copied.

## Project-owned observations — 2026-08-13

- A temporary logged-out profile navigated to public Weibo search and was redirected to `passport.weibo.com`; no result cards were present. The internal provider must therefore use `weibo/default` and return/wait on typed authentication state rather than reporting an empty successful scan.
- A temporary logged-out profile navigated to Tieba public search and received HTTP 403 with no recognized result items. The provider must classify this as session/challenge access, not EOF.
- After completing the public Tieba session in the application-owned profile, a value-free observation identified the search result boundary and stable semantic class names. The observation retained only class/tag signatures and the path shape `tieba.baidu.com/p/{numeric_id}`; it did not retain result text, IDs, account data or response bodies.
- The independently declared Tieba keyword contract uses the observed result container/card/link/title/body/reply-count landmarks. A rendered "load more" control belonged to a separate hot-topic block, so it was deliberately excluded instead of being misclassified as thread pagination.
- These observations retained only HTTP status, final host, selector counts and sanitized class/path shapes. No cookie, UID, nickname, text, request header or response body was retained.

## Implemented boundary

- A shared manual bootstrap opens each MediaCrawler-replacement source in its application-owned `source/default` profile. The user completes login/challenges and closes the tab; CBCE never reads or prints cookies.
- Weibo bootstrap now opens `s.weibo.com`, the same origin used by search, so Weibo can complete its own SSO flow before the profile is considered ready. A prior `weibo.com`-only login was correctly rejected by the search probe as `AUTH_REQUIRED`.
- Weibo targets support strict mobile detail/post and numeric creator forms. Tieba targets support thread, forum and creator forms. Both reject userinfo, nonstandard ports and hostile lookalike domains.
- Weibo post and Tieba thread contracts have independent versioned `term/page/offset` checkpoints, canonical identity validation, HMAC author pseudonyms and metric allowlists.
- Both sources have contract-driven DOM search providers with bounded selector payloads, exact offset/page/term pagination, fail-closed root recognition and manual-auth continuation. Tieba now has a default contract derived from the project-owned observation; Weibo intentionally has none until its authenticated probe succeeds.
- `cbce.dom-structure.v1` recursively observes regular/open-shadow DOM structure but reads only tag names, class names, attribute names and bounded counts. It never evaluates text, URL or attribute values; opaque/generated tokens are dropped before output.
- XHS/RedNote, Douyin and Kuaishou share a provider-neutral video normalization layer while retaining platform-specific URL parsers, provider implementations and metric policies.
- Tieba keyword search is wired through `cbce.worker.v1` and the shared bounded search executor under provider `cbce_tieba`. `CONTENT_BOT_CBCE_ENABLED=true` plus the explicit provider override now select it as the active search path; the legacy bridge remains present only for the rollback window.
- Two separate direct/worker canaries each returned four normalized public thread records and four reply-count metrics, then closed the owned browser/process tree. Coverage remains explicitly `partial_dom`: the observed page rendered four thread cards and exposed no trustworthy thread-list continuation.
- After registry wiring, the generic aggregate-only cutover runner also selected `cbce_tieba` explicitly and retained a structured canary with `2/2` unique threads, bounded requests/deadline and `persisted=false`. A second retained report separated by at least one hour is still required before changing the default provider.
- Project-owned value-free observations now cover one thread detail, one forum listing and one creator listing. Forum and creator each expose semantic `.thread-card` boundaries with canonical thread links; detail exposes a single public title/first-floor body boundary. No raw observed URL, ID, title, author or body was retained.
- Detail, forum scan and creator listing are wired through the same isolated worker/profile. Direct and worker canaries both returned aggregate counts `detail=1, forum=4, creator=4` with every terminal event equal to `complete`.
- Saved Tieba forum/creator URLs are canonicalized through the strict Tieba parser. The generic channel normalizer previously removed `?kw=...`; that bug is fixed and a thread URL is now rejected as a channel target.
- An ephemeral RunManager canary completed the real `channel → scanner → isolated worker → ingest` path with four fetched/new items in mongomock and no production persistence.
- Root comments use the project-observed `.pb-comment-item` boundary and the nearest numeric `data-id` as the stable provider identity. The first extraction guess incorrectly assumed `data-id` lived directly on `.virtual-list-item`; a live count-only canary exposed that mistake, and the corrected ancestor lookup returned five comments with five stable IDs through the isolated worker.
- Root comment coverage is explicitly `rendered_root_comments_only`. A bounded follow-up observation found rendered `.pb-lzl-item` child nodes, but those nodes had no numeric `id`/`data-id` of their own; their nearest numeric `data-id` was the root comment holder. The worker canary therefore failed closed at zero instead of reusing the root ID. Child comments remain planned and no positional/content-derived ID is synthesized.

## Weibo reviewed-contract runtime (2026-08-13)

- Weibo search is wired through its provider-neutral normalizer, contract-driven DOM provider, isolated worker, profile v2 and shared budget/checkpoint/auth lifecycle.
- Content Bot ships no Weibo selectors. A reviewed `weibo_post_v1` artifact is mandatory; missing/invalid provenance or selectors returns `setup_required`. The search URL is taken from the reviewed artifact but remains restricted to official Weibo HTTPS hosts.
- Implementation readiness does not satisfy cutover: the provider override, two separated aggregate canaries and authenticated cleanup evidence are still required.
- The worker-to-connector boundary now decodes comment events into the canonical `CommentRecord` instead of exposing raw dictionaries. Source/content/comment identity, UTC timestamp, nonnegative counters and bounded provenance are revalidated; terminal worker errors are propagated as typed failures instead of being treated as an empty successful scan.
- Stored Tieba threads can invoke the existing experimental root-comment operation through the shared manual comment API only when `cbce_tieba/list_comments` passes its runtime rollout gate. The explicit override must select CBCE for both source search and `list_comments`; selecting only one fails closed. Results use the shared Mongo hierarchy/upsert/retention/delete boundary. A declared child count marks the scan truncated because stable child identities are still unavailable.
- An aggregate-only shadow run retained no item payload and wrote nothing to Mongo/export. It ended as `BASELINE_TIMEOUT` after 60 seconds because the MediaCrawler baseline emitted no records; cleanup removed all child processes and scratch state. This is recorded as unavailable baseline evidence, not as candidate acceptance.

## Remaining gate

- Authenticated, sanitized Weibo DOM observation generated by this project, followed by search/detail/creator/root-comment contracts and explicit partial-child coverage.
- Tieba child-comment contract with stable provider child identity and bounded pagination. Root comments are implemented/partial; the current authenticated render did not expose child nodes.
- Tieba minimum-operation cutover evidence now consists of two aggregate-only 2/2 canaries separated by 3666 seconds, configured active-provider resolution and passing clean-room/rollback audits. The non-responsive legacy overlap remains recorded as an evidence limitation; it does not justify claims beyond the partial DOM coverage in the manifest.
- Child-comment persistence remains disabled until an independently observed stable child identity exists; root IDs, indexes or body hashes are not substituted.

## Licensed Weibo runtime hardening — 2026-08-15

- `licensed_weibo` remains explicit opt-in and manual-only. Its selected provider identity now survives the connector → isolated worker → run context → normalized record boundary; a mismatched identity fails before browser navigation.
- The operation advertises and enforces `100` items / `100` requests per run. Larger topic budgets are reduced before the worker starts and are recorded as a run warning instead of failing inside the licensed facade.
- Disabling non-commercial reuse returns `disabled_by_policy` with `LICENSED_REUSE_POLICY`; source catalog and run planning remain available and no licensed worker is opened.
- Mobile API pagination respects the terminal `has_more` signal, preserves same-page offsets and moves directly to the next search term without a speculative empty request.
- Licensed source-map entries now bind local content with SHA-256 and require the exact upstream snapshot commit, unique safe paths and timezone-aware review timestamps. Cutover audit applies both this integrity check and the runtime reuse policy.
- A bounded live canary on 2026-08-15 (`2` items / `2` requests / `45s`) reached the licensed worker and returned typed `AUTH_REQUIRED` with zero observed or persisted items. A subsequent manual-login window remained unauthenticated and ended `DEADLINE_EXCEEDED`; heartbeat remained healthy. Reports are aggregate-only under `data/cbce-canary-reports/`; no provider cutover is claimed until an operator completes Weibo login and repeats the canary successfully.
- The worker protocol now emits a bounded heartbeat every 10 seconds while an owned browser is waiting for login, so the parent supervisor does not misclassify a legitimate interactive session as a transport failure.
- The post-heartbeat bounded canary (`licensed-weibo-2026-08-15-e.json`) also ended deterministically as `DEADLINE_EXCEEDED` with `0` items and `persisted=false`; there was no `Worker heartbeat timed out`. This confirms the remaining gate is external session authorization, not worker supervision.

## RunManager dedupe evidence — 2026-08-13

- A deterministic Tieba regression runs the same canonical thread through two completed batches. The second observation updates the stored metric and appends a trend snapshot but keeps one content item, one keyword match and `new_item_count=0`.
- The regression also found and fixed two shared checkpoint/counting defects: a versioned checkpoint with a changed query fingerprint no longer inherits stale `recent_ids` as though it were legacy data, and an existing keyword match no longer increments `ingested_count` when it is refreshed.
- Full backend regression excluding subtitle tests: `410 passed, 1 skipped`. No subtitle implementation or test was edited by this crawler change.
