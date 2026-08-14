# YouTube provider provenance

## Provider and access boundary

- Content Bot uses only the official YouTube Data API v3 with an API key for public keyword search, video details/statistics, channel resolution, uploads-playlist scans, and bounded public comment trees.
- It does not use browser cookies, account passwords, undocumented endpoints, signing code, or MediaCrawler implementation details.
- HTTP transport is fixed to `https://www.googleapis.com/youtube/v3`, disables redirects, and never puts the key in logs, raw payloads, checkpoints, or error messages.

## Implemented behavior

- Local health reports only whether configuration exists. `GET /api/v1/sources?deep=true` explicitly performs `i18nLanguages.list`, a documented one-unit read per attempt with bounded retries, to classify invalid access, exhausted quota/rate limits, transport degradation, or success without consuming a `search.list` request.
- Keyword search is newest-first within the configured rolling 90-day window. Its checkpoint fingerprint includes region, relevance language, ordering, window policy, and provider-contract version.
- Every recurring keyword or saved-channel scan starts at the newest page. It resumes an older backlog cursor only after the frontier overlaps previously observed canonical video IDs, preventing a continuation cursor from hiding newly published videos.
- Expired `invalidPageToken` cursors are discarded safely. Natural overlap clears the backlog; item or quota-budget exhaustion preserves the next unprocessed cursor.
- Search and general-read request buckets have separate per-run budgets. Defaults are 10 `search.list` requests and 25 general read requests and can be reduced through environment configuration.
- Search/channel paths share canonical video IDs, normalized hashtags, metric semantics, and a minimized raw allowlist.
- Stored videos can be scanned manually with `commentThreads.list(part=snippet,textFormat=plainText)`. Every top-level thread uses the provider's `totalReplyCount`; replies are fetched from `comments.list(parentId=...)` instead of assuming the limited embedded `replies.comments` list is complete.
- Root, reply-per-root, total, and physical request budgets are independent. Root/reply page tokens have repeated-cursor guards, `commentsDisabled` is typed as unsupported, and partial results explicitly report `truncated`.
- Comment authors use only `authorChannelId.value` as transient HMAC input. Display names, channel objects, API responses, and the API key are not persisted. Normalized comments share the comment collection/API and content-deletion/retention boundary with Reddit.

## Remaining gates

- Live key validation and quota behavior must be canaried against the project's actual Google Cloud quota allocation before production scheduling.
- Media download remains a separate operation and is not implied by comment/video metadata support.
- The rolling window and request budgets are intentionally partial coverage and remain visible in the source manifest/UI disclaimer.

## Primary references

- YouTube Data API overview: https://developers.google.com/youtube/v3/getting-started
- `search.list`: https://developers.google.com/youtube/v3/docs/search/list
- `commentThreads.list`: https://developers.google.com/youtube/v3/docs/commentThreads/list
- `comments.list`: https://developers.google.com/youtube/v3/docs/comments/list
- Comment thread resource and embedded-reply limitation: https://developers.google.com/youtube/v3/docs/commentThreads
- `i18nLanguages.list`: https://developers.google.com/youtube/v3/docs/i18nLanguages/list
- Quota calculator: https://developers.google.com/youtube/v3/determine_quota_cost
- Error reference: https://developers.google.com/youtube/v3/docs/errors
