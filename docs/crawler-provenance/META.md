# Meta provider provenance ledger

Status: Instagram hashtag discovery and the explicitly authorized Facebook Page feed provider were implemented on 2026-08-13. Approved Graph access, permission review and bounded live canaries remain external gates. Instagram owned-account/Business Discovery and Facebook PPCA providers remain planned.

## Boundary

- Provider code in `backend/app/crawlers/adapters/instagram/official_provider.py` and `backend/app/crawlers/adapters/facebook/official_provider.py` is independent official-API implementation.
- No MediaCrawler source, selector, request-signing code, payload fixture or cookie flow was used. MediaCrawler does not supply the Meta provider.
- There is no browser/cookie fallback. Missing App Review, Advanced Access, Professional-account authorization or token state must remain a typed permission/setup failure.

## Primary official references

- Instagram API with Facebook Login: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/get-started/>
- Instagram Hashtag Search: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/hashtag-search/>
- Instagram Business Discovery: <https://developers.facebook.com/docs/instagram-platform/instagram-api-with-facebook-login/business-discovery/>
- Pages API posts/feed: <https://developers.facebook.com/docs/pages-api/posts/>
- Pages API getting started and Page tokens: <https://developers.facebook.com/docs/pages-api/getting-started/>
- Page feed reference: <https://developers.facebook.com/docs/graph-api/reference/page/feed/>
- Page Public Content Access: <https://developers.facebook.com/docs/features-reference/page-public-content-access/>
- Meta permissions reference: <https://developers.facebook.com/docs/permissions/>
- App Review: <https://developers.facebook.com/docs/app-review/>
- Access levels: <https://developers.facebook.com/docs/graph-api/overview/access-levels/>
- Platform Terms: <https://developers.facebook.com/terms/>

The Meta documentation host returned HTTP 429 to the latest automated verification attempt on 2026-08-13. The code contracts are marked `implemented`, but runtime availability remains `setup_required` until their pinned Graph version, approved credentials and authorized asset identifiers are configured. Release still requires a human review of the selected current version/scopes and bounded live canaries using authorized test assets.

## Implemented Instagram hashtag contract

- Explicitly pinned Graph API version; no implicit `latest` version.
- Bearer token is sent in the Authorization header, never in URL parameters, worker payloads or logs.
- Hashtag ID lookup followed by bounded `recent_media` pagination.
- Versioned per-term checkpoint containing only provider cursor and public hashtag identity.
- Canonical Instagram permalink, Graph media ID, caption, timestamp, like/comment counts and HMAC author pseudonym.
- Ephemeral media/thumbnail URLs are deliberately not persisted by this contract.
- Coverage is marked `best_effort`; it is not advertised as full Instagram search or a firehose.
- Typed auth, permission, rate-limit, not-found, transport and parse-change errors; provider response detail is not exposed.
- Persistent rolling unique-hashtag budget per Professional account, with a configurable implementation cap of 30 unique terms over seven days. The ledger stores SHA-256 term digests, timestamps and public hashtag IDs, never raw terms or credentials.
- Hashtag IDs are cached across runs, avoiding repeated lookup calls. Corrupt ledger state fails closed instead of silently resetting quota accounting.
- The Content Bot connector is bound to manifest operation `instagram_hashtag/search`; natural exhaustion clears the cursor and the checkpoint provenance names the actual operation provider rather than the source-wide default.

## Implemented authorized Facebook Page contract

- The provider reads only the feed of the one Page explicitly configured by numeric Page ID and username. It is not global Facebook search, personal-profile/Group ingestion or Page Public Content Access.
- Graph API version is pinned explicitly and the Page access token is sent only in the Authorization header. The exact Graph host is enforced and redirects are disabled.
- The bounded `/{page-id}/feed` contract requests an explicit field allowlist and carries a versioned provider cursor through the existing durable channel checkpoint flow.
- Channel targets must resolve to the configured Page ID or username. Post, reel, video, watch and short-link targets are rejected as channels.
- Normalized output keeps the stable Graph post ID, canonical HTTPS Facebook permalink, message/timestamp, allowlisted reaction/comment/share metrics and an HMAC author pseudonym. Raw Page names, access tokens, media URLs and provider responses are not persisted.
- Auth, permission/App Review, rate-limit, not-found, transport and parse failures are typed and redact provider response detail.
- Manifest operation `meta_pages/scan_channel` is `implemented/partial` and background-safe only after local configuration is complete. Facebook global search, PPCA, profiles, Groups, detail and comments remain disabled until separate contracts and access reviews exist.
- There is no browser/cookie fallback.

## Remaining acceptance gates

1. Confirm exact fields and permissions against the selected current Graph API version.
2. Record App Review/Advanced Access basis and authorized Instagram Professional account ID outside source control.
3. Run sanitized live contract observations and a very small recent-media canary.
4. Run a bounded authorized-Page feed canary and confirm Page task/permission/App Review behavior, including token revoke and an unauthorized Page target.
5. Keep Instagram owned Professional, Business Discovery and comments plus Facebook PPCA/detail/comments disabled until each has its own permission review, adapter and test asset.
6. No automatic browser/cookie fallback is allowed.
