# TikTok official provider provenance

Status: Phase 9 Display API, encrypted token vault, automatic refresh and interactive Web Login Kit callback/UI implemented; external app approval and live canary pending, 2026-08-13.

## Access model

- TikTok Login Kit is OAuth 2.0. The web authorization endpoint is `https://www.tiktok.com/v2/auth/authorize/`; server-side token exchange, refresh and revoke use `https://open.tiktokapis.com/v2/oauth/`.
- Display API requires user authorization. `video.list` reads only the public videos of the TikTok account that granted the token; it is not global keyword search and cannot enumerate an arbitrary creator.
- `/v2/video/list/` returns newest-first authorized-account videos with an opaque millisecond timestamp cursor and a maximum of 20 records per page.
- `/v2/video/query/` accepts at most 20 video IDs and returns data only for videos belonging to the authorized user.
- Research API uses a separate client token and `research.data.basic`, requires an approved research project, and remains disabled.

## Implemented clean-room boundary

- Server-side authorization URL construction with unpredictable state validation; the client secret never enters the URL.
- Browser OAuth start/callback uses a one-time bounded server state plus an HttpOnly, Secure, SameSite=Lax cookie. Start and callback must share the approved HTTPS origin; state is consumed on success or denial and cannot be replayed.
- OAuth requests `user.info.profile` in addition to `user.info.basic` and `video.list`; after token exchange the backend calls official `/v2/user/info/`, verifies `open_id` and the real username against the requested creator, and revokes a mismatched grant before storing anything.
- Typed authorization-code exchange, refresh-token rotation and revoke requests. Tokens are hidden from object representations and sent only in form bodies or Bearer headers to the exact official host; redirects are disabled.
- Display list/detail providers with exact 20-item request caps, versioned cursor, HMAC creator pseudonym, stable video identity, UTC timestamps, metric allowlist and typed auth/scope/rate/transport/parse failures.
- Content Bot connector and channel scanner accept only the configured `@username` that owns the authorized `open_id`. Other creators, video URLs and short URLs cannot be saved as Display API channels.
- Expiring cover-image URLs and embed HTML are deliberately not persisted. Global search and comments are not advertised through Display API.

## Runtime gate

- Static manifest exposes `scan_channel` and `fetch_detail` as implemented/partial under `tiktok_display`, but runtime availability remains `setup_required` until an approved app supplies an access token, open ID, authorized username and granted `video.list` scope.
- Refresh and access tokens can now be stored in a provider-specific AES-256-GCM vault below the application data directory. The key and ciphertext are separate owner-restricted files; writes are atomic, encrypted payload tampering fails closed, and tokens never enter MongoDB, process arguments or logs.
- The TikTok connector prefers the encrypted vault, refreshes a near-expiry access token through the official server-side OAuth endpoint, persists a rotated refresh token atomically, and remains limited to the username/open ID that granted access.
- The source card now follows manifest `primary_operation=scan_channel`, shows OAuth rather than browser/public auth, accepts the approved creator handle, starts Login Kit, reports callback outcomes without tokens, supports explicit refresh, and confirms before revoke/delete.
- Environment token fields remain a compatibility path and are not migrated into the vault automatically because they do not include trustworthy issuance/expiry timestamps.
- A live callback/list/detail canary remains blocked on an approved TikTok app, registered HTTPS callback, and opted-in test creator. Research API approval is a separate gate.
- Research search/comments remain planned and disabled; there is no browser fallback.

## Official references

- Login Kit overview: https://developers.tiktok.com/doc/login-kit-overview
- Login Kit for Web: https://developers.tiktok.com/doc/login-kit-web
- User access token lifecycle: https://developers.tiktok.com/doc/oauth-user-access-token-management
- Display API overview: https://developers.tiktok.com/doc/display-api-overview/
- List videos: https://developers.tiktok.com/doc/tiktok-api-v2-video-list/
- Query videos: https://developers.tiktok.com/doc/tiktok-api-v2-video-query/
- Video object: https://developers.tiktok.com/doc/tiktok-api-v2-video-object/
- API v2 errors: https://developers.tiktok.com/doc/tiktok-api-v2-error-handling/
- Research video query: https://developers.tiktok.com/doc/research-api-specs-query-videos/
