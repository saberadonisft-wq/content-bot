# Bluesky provider provenance

## Provider and access boundary

- Content Bot uses Bluesky's two documented direct AppView hosts, preferring the cached `https://public.api.bsky.app` host and allowing one bounded failover to exact `https://api.bsky.app` when the cached edge returns HTTP 403. The allowlisted operations are `app.bsky.feed.searchPosts`, `app.bsky.feed.getAuthorFeed`, `com.atproto.identity.resolveHandle`, and `app.bsky.feed.getPostThread`.
- It does not use browser cookies, account passwords, private PDS data, undocumented signing, fingerprint spoofing, or MediaCrawler code and fixtures.
- Requests are restricted to the required public `app.bsky.*` namespace plus exact `com.atproto.identity.resolveHandle`, redirects are disabled, and the client identifies itself as Content Bot rather than a consumer browser.
- Public AppView discovery is best effort. It is not described as a complete firehose or a guarantee that every federated record is indexed and returned.

## Identity and normalized data

- A post must carry a valid `at://<did>/app.bsky.feed.post/<rkey>` URI. The canonical Content Bot external ID is a stable digest of that AT URI.
- The CID is retained only as the bounded content version. Editing a record can change its CID without changing its Content Bot identity.
- Public handles may be used for display and canonical `bsky.app` links. The underlying DID and full author/profile blobs are excluded from persisted raw payloads.
- Reply parent/root and quoted-record AT URIs are converted into stable pseudonymous relation IDs. Repost discovery is represented explicitly without retaining the source feed-view/profile object.
- Image, video, and external-card embeds accept HTTPS URLs only and enforce count and field-length bounds. Exact like, reply, repost, and quote counts remain separate in the raw allowlist; legacy storage mapping is explicit.

## Checkpoint, pagination, and failure behavior

- Search cursors are scoped independently to each normalized term and its query fingerprint.
- Every recurring search or saved-author scan begins at the newest frontier. An older backlog cursor is used only after the frontier overlaps recently observed canonical IDs.
- Invalid cursors are cleared narrowly, natural exhaustion tombstones the continuation, and repeated cursors stop instead of looping.
- Author feeds paginate beyond one page and preserve reply/repost filter semantics. A page whose entries are all filtered reposts is not treated as false end-of-feed when a next cursor exists.
- Per-run request budgets bound both keyword and author-feed traversal. Transport errors and 408/425/429/5xx responses retry with bounded backoff; permission, not-found, invalid-request/cursor, rate-limit, transport, and parse failures remain typed and redacted.
- The direct-host failover consumes a physical request from the same budget, happens at most once, and never follows a provider-controlled hostname or redirect.

## Bounded public reply trees

- A stored post can be scanned manually through `getPostThread`. A canonical `bsky.app/profile/<actor>/post/<rkey>` URL is resolved to an AT URI; handles use the public `resolveHandle` query while an explicit DID needs no resolution call.
- The documented AppView tree has a depth parameter but no reply pagination cursor. The adapter therefore treats it as bounded partial coverage: every missing/blocked node, declared count above rendered children, repeated identity, depth limit, root/child/total limit, or physical request limit leaves `truncated=true`.
- Root, descendants-per-root, total, depth, and physical HTTP attempts are limited independently. The response root URI and every reply's record-level parent/root URI must match the requested tree before any record reaches persistence.
- Persisted content, comment, parent, and root identities are stable digests of valid post AT URIs. Raw AT URIs and DIDs remain transient; only a DID passes through the HMAC pseudonymizer to produce the stored author pseudonym.
- Provider ordering is retained explicitly as `provider_defined`; the API does not advertise selectable top/new sorting that `getPostThread` does not expose.

## Remaining gates

- On 2026-08-13 the cached public hostname returned HTTP 403 from the deployment network while the documented direct AppView returned HTTP 200. A regression test now fixes the one-time failover boundary, and an aggregate-only canary subsequently passed with `2/2` unique records and no persisted content.
- Periodic opt-in live canaries should verify the current public AppView response contract and rate behavior before changing production budgets.
- Search, author feeds, and bounded public thread expansion remain partial surfaces. Authenticated/private surfaces and media-byte download are not implied by the current implementation.

## Primary references

- AT Protocol reading-data guide: https://atproto.com/guides/reading-data
- Bluesky `searchPosts` reference: https://docs.bsky.app/docs/api/app-bsky-feed-search-posts
- Bluesky `getAuthorFeed` reference: https://docs.bsky.app/docs/api/app-bsky-feed-get-author-feed
- Bluesky `getPostThread` reference: https://docs.bsky.app/docs/api/app-bsky-feed-get-post-thread
- AT Protocol `resolveHandle` Lexicon: https://github.com/bluesky-social/atproto/blob/main/lexicons/com/atproto/identity/resolveHandle.json
- Bluesky viewing-feeds guide: https://docs.bsky.app/docs/tutorials/viewing-feeds
- Bluesky API hosts and authentication guide: https://docs.bsky.app/docs/advanced-guides/api-directory
- Bluesky AppView rate-limit guide: https://docs.bsky.app/docs/advanced-guides/rate-limits
