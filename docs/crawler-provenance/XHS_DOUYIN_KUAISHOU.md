# XHS, Douyin and Kuaishou provider provenance

Status: Phase 7 authentication-gated, 2026-08-13. Strict target parsers, provider-neutral records, shared bounded DOM lifecycle and value-free live probes exist. No source has an executable clean-room search contract yet because the application-owned profiles have not rendered authenticated search results.

## MediaCrawler behavioral specification retained

- Each source follows the useful high-level lifecycle: application-owned persistent profile → visible/manual authentication → bounded search/detail/creator/comments → normalization → Content Bot persistence.
- XHS/RedNote is domain-aware; Douyin and Kuaishou keep separate target, metric and media policies. TikTok is never treated as an alias for Douyin.
- Challenge/CAPTCHA handling is a typed pause for the user. The new crawler does not automate sliders or other challenges.

These are behavioral requirements, not copied implementation. No XHS signing wrapper, Douyin `a_bogus` code, Kuaishou signature capture hook, internal endpoint payload, selector, stealth script, challenge trajectory, normalizer or fixture is reused.

## Project-owned observation method

- `cbce.dom-structure.v1` runs only in `source/default` profiles owned by Content Bot. It returns final host, bounded tag/class/attribute-name counts and open-shadow-root count.
- Follow-up contract checks emit only counts for canonical public path shapes such as `/explore/`, `/video/` and `/short-video/`. They do not emit URLs, IDs, text, nickname, cookie, header or response body.
- Probe targets are the public search pages for the neutral term `game`; each navigation is HTTPS-only and constrained to the source's exact domain boundary.

## Live observations — 2026-08-13

- XHS reached `www.xiaohongshu.com` with 1,360 DOM elements but rendered zero `/explore/` links, zero note-item landmarks and six login controls. This is an authentication surface, not an empty successful search.
- Douyin reached `www.douyin.com` with 518 DOM elements but rendered zero `/video/` links. It exposed dynamic class names, so no selector is retained from the logged-out shell.
- Kuaishou reached `www.kuaishou.com` with 220 DOM elements. A semantic video-list shell existed, but it contained zero `/short-video/` links; its eight `a.item` nodes were outside the video list and therefore are not content cards.
- All three owned browsers closed cleanly after observation. No default search selector contract was declared and all manifest operations remain planned/disabled.

## Reviewed-contract runtime (2026-08-13)

- Search now has a real clean-room handler and isolated-worker lifecycle for all three sources. No selector is bundled: availability remains `setup_required` until a reviewed `cbce.observed-dom-search.v1` artifact exists in the configured contract root.
- The artifact is source/provider-bound, value-free, size/host/schema/provenance checked and cannot contain challenge automation. Provider selection remains an explicit rollback flag; legacy stays active until two live canaries and the remaining cutover gates pass.
- The shared contract and review workflow is documented in `OBSERVED_DOM_CONTRACTS.md`. This changes implementation state, not evidence state: the earlier logged-out observations still do not authorize any selector.

## Implemented boundary

- Strict URL parsers distinguish content, creator and short-link targets, enforce DNS boundaries and reject userinfo/nonstandard ports.
- `BrowserVideoDomSearchProvider` supplies source-bound `term/page/offset` cursors, exact page budgets, typed parse drift, manual-auth continuation, HMAC author pseudonyms, metric allowlists and allowlisted media metadata.
- Shared navigation now detects both cross-domain login redirects and source-specific same-host login gates. The independently observed gates are XHS's QR login container, Douyin's visible telephone-login form and Kuaishou's login detail panel. Detection has a bounded grace window for delayed modals, emits typed auth events and never reads credentials or automates a challenge.
- XHS, Douyin and Kuaishou still require source-specific URL builders, selectors, media roots and normalizers before worker binding. The shared provider is lifecycle infrastructure, not evidence that their platform contracts are interchangeable.

## Remaining gate

- Complete visible login in each Content Bot-owned profile and repeat the value-free observation.
- Declare selectors only from authenticated project observations, then run direct and isolated-worker canaries twice.
- Add source-specific detail/creator/root-child comment contracts, exact caps, cursor-stall guards and challenge/cancellation tests.
- Obtain aggregate shadow evidence against a responsive legacy baseline before any default cutover; keep MediaCrawler as rollback until each source independently passes.

## Latest gate verification

- Bounded probes now return typed `AUTH_TIMEOUT` for XHS, Douyin and Kuaishou instead of treating the logged-out shell as a successful empty result.
- The 2026-08-13 repeat probe added value-free canonical path counters. XHS rendered zero `/explore/` links and a project-observed login button, so the same-host login sentinel now includes that button and the profile remains authentication-gated; it is not treated as an empty successful search.
- Same-host auth detection and redirect auth share the same cancellation/deadline boundary; focused runtime/probe tests pass.
- Full backend regression excluding subtitle scope: `412 passed, 1 skipped`. No subtitle file was changed by this crawler slice.
