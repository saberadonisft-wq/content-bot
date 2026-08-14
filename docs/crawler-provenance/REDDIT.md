# Reddit provider provenance

## Access boundary

- Content Bot uses Reddit application-only OAuth with the `client_credentials` grant for read-only public submission discovery and bounded public comment-tree reads.
- The token grant is sent to `https://www.reddit.com/api/v1/access_token` with HTTP Basic authentication. Bearer-authenticated data requests are sent only to `https://oauth.reddit.com`.
- Client credentials and access tokens remain in process memory/configuration, are excluded from representations and provider error messages, and are never put in URLs, checkpoints, raw payloads, or command-line arguments.
- The connector does not use browser cookies, password login, or MediaCrawler code.

## Implemented behavior

- One shared, concurrency-safe token cache serves keyword and saved-channel scans. It refreshes shortly before expiry, isolates cache entries by a one-way credential fingerprint, retries bounded transient failures, and classifies auth, rate-limit, transport, and response-shape errors without leaking provider details.
- Keyword search persists a separate `after` cursor for every normalized term. Saved subreddit/user feeds persist a target-scoped cursor and paginate beyond the first page within the run budget.
- Global and channel paths share the same HMAC author pseudonym policy and raw-payload allowlist. Raw usernames and full Reddit response objects are not persisted.
- Reddit `score` remains available with its exact semantics in the normalized metric descriptor/raw allowlist. The compatibility storage key remains `like_count` until the metric-schema migration is applied.
- Stored Reddit posts can be scanned manually through the documented comment-tree endpoint. `more` stubs are expanded through `/api/morechildren` sequentially, with at most 100 child IDs per call and independent root, children-per-root, total, depth, and physical request budgets.
- Comment identities use Reddit comment fullnames without the `t1_` prefix. Parent/root relations are validated before persistence, usernames are converted to the shared HMAC pseudonym, and provider response/account blobs are not retained.
- Comments use an idempotent `(source_id, external_id)` Mongo identity and are anchored to the canonical content item. Item retention, item deletion, source deletion, pruning, backup, and restore therefore include the normalized comment collection.

## Remaining gates

- Live credentials must be tested against the registered Reddit application and its approved use case before rollout.
- Quarantined, private, banned, or permission-restricted communities still need dedicated typed-error fixtures/live canaries.
- Quarantined/private/banned live cases still require approved-account canaries; current 401/403/404/429/5xx and malformed-shape behavior is covered deterministically and fails with typed errors.
- Product use must continue to satisfy the current Reddit Developer Terms and Data API Terms; working credentials do not imply authorization for every commercial or research use case.

## Primary references

- Reddit OAuth2: https://github.com/reddit-archive/reddit/wiki/oauth2
- Reddit API reference: https://www.reddit.com/dev/api/
- Reddit `/comments/{article}` reference: https://www.reddit.com/dev/api/#GET_comments_{article}
- Reddit `/api/morechildren` reference: https://www.reddit.com/dev/api/#GET_api_morechildren
- Reddit Data API Terms: https://redditinc.com/policies/data-api-terms
- Reddit Developer Terms: https://redditinc.com/policies/developer-terms
