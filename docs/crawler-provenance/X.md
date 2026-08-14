# X provider provenance

Status: Phase 11 official read-only provider, 2026-08-13. Official recent search, saved/standalone creator timeline, post detail and bounded recent-conversation replies are implemented but remain setup/access/credit-gated. Public timeline embed remains an independent view-only operation.

## Official evidence reviewed

- [X API Search Posts](https://docs.x.com/x-api/posts/search/introduction): recent search covers the most recent seven days, supports pagination and up to 100 posts per request; full-archive access is separate.
- [X API Post lookup](https://docs.x.com/x-api/posts/get-post-by-id): official `GET /2/tweets/{id}` read contract and selectable post/media fields.
- [X API User Posts](https://docs.x.com/x-api/users/get-posts): user-post pagination, `since_id`, `until_id`, exclusions and field expansions.
- [X API response codes](https://docs.x.com/x-api/fundamentals/response-codes-and-errors): 401/403/404/429/5xx semantics, structured problem types, partial errors and rate-limit headers.
- [X API rate limits](https://docs.x.com/x-api/fundamentals/rate-limits): endpoint-specific windows and `x-rate-limit-reset`; billing and rate limits are separate controls.
- [X API pricing](https://docs.x.com/x-api/getting-started/pricing) and [usage/billing](https://docs.x.com/x-api/fundamentals/post-cap): API v2 reads are credit-based/pay-per-use and have usage caps.

## Implementation boundary

- `x_api` is official API only. It never posts, likes, follows, sends messages or falls back to browser scraping.
- `X_BEARER_TOKEN` is read from backend settings and sent only in the `Authorization` header. It is not stored in manifests, checkpoints, output records, event payloads or error messages.
- Recent keyword search is explicitly `partial` because it is limited to X's recent-search window and the application's current access/credits.
- Search uses a versioned per-term cursor. If the API minimum page size returns more rows than the remaining item budget, the cursor continues using the last emitted Post ID rather than the provider's next token; this prevents silently skipping paid-for rows.
- Saved X creator URLs remain viewable through the public embed without credentials. When `X_BEARER_TOKEN` is configured, the same saved channel can run the official User Posts endpoint through manifest operation `x_api/scan_channel`.
- Creator checkpoints are scoped to the normalized username. Reply/repost inclusion flags are sent as official timeline exclusions and are part of the durable checkpoint fingerprint, so changing channel filters cannot resume an incompatible cursor.
- The run planner does not queue an API channel run when the token is absent; it degrades to embed-only instead of opening a client and failing the batch.
- Post identity is the official numeric ID with canonical URL `https://x.com/i/web/status/{id}`. Author IDs become source-scoped HMAC pseudonyms. Display names/profile blobs are not retained.
- Media is metadata only: validated public HTTPS image/preview URLs, dimensions and bounded duration. No media download is enabled.
- `fetch_post`, creator posts and recent conversation-reply client methods have operation-specific adapters, strict X target canonicalization, versioned checkpoints and root/parent reply hierarchy. Standalone detail, creator and comment handlers are registered as manual-only operations; scheduled pay-per-use execution remains disabled.
- Conversation replies are normalized parent-first before shared Mongo persistence. A direct reply uses the stored post ID as its hierarchy anchor; unresolved/orphaned nodes are not persisted and make the scan explicitly truncated.

## Safe error behavior

- 401 → `AUTH_REQUIRED`.
- 402 or structured `usage-capped` → `PAYMENT_OR_ACCESS_REQUIRED` with Developer Console CTA.
- 403 → `PERMISSION_REQUIRED`.
- 404 → `NOT_FOUND`.
- 429 rate-limit → `RATE_LIMITED`, using `x-rate-limit-reset` when valid.
- 5xx/transport → retryable `TRANSPORT_ERROR`.

Raw provider problem details, request URLs and bearer tokens are never embedded in the safe failure. There is no automatic upgrade, purchase or browser fallback.

## Validation evidence

- Mock-transport tests cover the official route/fields, API minimum page size, payload normalization, media, HMAC privacy, cursor exactness and all error classes above.
- Catalog tests prove `render_embed` remains ready independently, while official search and saved-channel scan are implemented but setup-required without `X_BEARER_TOKEN`.
- Connector and RunManager tests prove account-only target validation, include-reply/repost mapping, HMAC privacy, `x_api` checkpoint provenance, and no queued channel run without credentials.
- Focused X/registry/comment persistence gate passed: 49 tests.
- Full backend crawler regression excluding subtitle/Gemini/text suites passed: 669 passed, 2 opt-in tests skipped.

## Remaining gate

- Configure an eligible X developer app/token and explicit spend/quota policy.
- Run bounded live canaries for search, saved creator timeline and lookup without persisting raw response artifacts.
- Obtain two bounded passing live canaries separated by the cutover audit interval. The current deployment canary returns typed `PAYMENT_OR_ACCESS_REQUIRED`, so no live-read claim is made.
- Keep scheduled pay-per-use search disabled/manual by default until a per-run cost budget and operator opt-in exist.
