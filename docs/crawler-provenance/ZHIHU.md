# Zhihu provider provenance

Status: Phase 8 challenge-gated, 2026-08-13.

## MediaCrawler behavior retained as specification

- Search, answer/article/zvideo detail, creator content and bounded root/child comments remain required capabilities.
- The implementation must keep one application-owned browser profile, typed authentication/challenge states, exact budgets and `content_id` storage parity.
- These are behavioral requirements only. No `x-zse`/`x-zst` implementation, vendor JavaScript, internal endpoint wrapper, selector, payload, fixture or normalizer is copied.

## Project-owned observation

- The neutral HTTPS probe targets `www.zhihu.com/search?type=content&q=game` inside the isolated `zhihu/default` profile.
- The first live navigation returned HTTP 403 and was classified as typed `CHALLENGE_REQUIRED` before any DOM contract was declared.
- A repeat on 2026-08-13 reached Zhihu's same-host `unhuman` challenge shell: the value-free structure contained the independently observed `div.unhuman` sentinel and zero `/question/`, `/p/` or `/zvideo/` content links. The probe now classifies that sentinel as `CHALLENGE_REQUIRED` instead of accepting the challenge DOM as a successful search page.
- No response body, cookie, header, account value or page text was retained. The browser and owned profile handle closed normally.

## Implemented boundary and next gate

- Strict answer/article/zvideo target parsing already exists and rejects cross-domain or malformed targets.
- The value-free DOM probe now includes Zhihu and reports typed failures without traceback.
- The source remains planned/disabled. A user must complete the visible challenge/login in the application-owned profile before search/detail/comment selectors can be observed and implemented.

## Reviewed-contract runtime (2026-08-13)

- Search now has a real contract-driven isolated-worker handler, but no selector ships with the project. Without an independently observed and reviewed artifact the operation is `setup_required`; detail/creator/comments remain planned.
- Mixed search identities are namespaced as `answer:<id>`, `article:<id>` and `video:<id>` to prevent numeric cross-entity collisions. Same-host login selectors only detect and pause for visible user action; they never automate a challenge.
- Provider selection and two separated live canaries remain cutover gates, so legacy continues as the active rollback provider.
