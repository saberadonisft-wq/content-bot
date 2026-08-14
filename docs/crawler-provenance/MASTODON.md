# Mastodon provider provenance

## Provider and access boundary

- Content Bot uses documented public Mastodon REST surfaces on exact hosts configured in `MASTODON_INSTANCES`: hashtag timelines, account lookup, account statuses, status/context reads, and explicit instance metadata health probes.
- Hashtag discovery is instance-scoped and best effort. It is not general full-text search and no configured instance is treated as representative of the whole Fediverse.
- Public preview can be disabled by an instance. HTTP 401 is reported as an app-token/auth requirement; Content Bot does not fall back to browser cookies or login automation.
- Instance configuration accepts public HTTPS origins only. Saved-account targets must be one profile on an allowlisted instance; arbitrary `/@` websites and status URLs are not Mastodon channel targets.

## Identity and normalized data

- A status uses its canonical origin `uri` as the stable identity when it is a valid HTTPS URL, with origin `url` as the public canonical link. A remote server's local status ID and the fetching instance never define identity.
- A boost/reblog wrapper is unwrapped to the original status before identity, text, metrics, media, and author display are normalized. Discovery records that the item was observed as a reblog.
- Reply state is retained, but instance-local reply IDs are converted into bounded pseudonymous relation IDs. Full account objects and local account IDs are excluded from raw payloads.
- Status HTML is parsed with Python's standard `HTMLParser`, which decodes entities and preserves block boundaries without executing markup.
- Media URLs must be HTTPS and are limited by item count and field lengths. Favourite, reply, and reblog counts remain exact in the raw allowlist while legacy normalized metric names stay explicit.

## Checkpoint, health, and failure behavior

- Hashtag cursors are scoped by instance and normalized hashtag. Account cursors additionally include the account and reply/reblog filter policy.
- Pagination accepts `max_id` only from the documented HTTP `Link` header with `rel=next`; missing Link means natural exhaustion and repeated cursors stop with a warning.
- A per-run request budget bounds all instance traversal. Transient transport and 408/425/429/5xx failures retry with bounded backoff; auth, permission, not-found, rate, transport, budget, and parse outcomes remain typed and redacted.
- Failure of one configured instance emits a visible partial-result warning and allows other instances to continue. If every request fails, the source run fails rather than returning a misleading empty success.
- Default catalog health validates local instance configuration only. Explicit `GET /api/v1/sources?deep=true` probes each configured `/api/v2/instance`; partial and total outages return `degraded` with safe reason codes.

## Bounded public status contexts

- Manual reply scans accept only canonical public status or ActivityPub status URLs on an exact configured instance. Content Bot first reads `GET /api/v1/statuses/:id` to verify the root/local ID and canonical origin identity, then reads `GET /api/v1/statuses/:id/context` for descendants.
- The Context entity has no descendant pagination cursor. Coverage is therefore explicitly partial: root, descendants-per-root, total, depth and every physical HTTP attempt are bounded; omitted/orphaned nodes, declared-count gaps, cycles, duplicates and budget truncation never become an unqualified complete result.
- Context descendants are reordered only enough to persist every parent before its child. Each local `in_reply_to_id` must resolve inside the requested tree; external comment/parent/root identities continue to derive from public canonical status URIs rather than instance-local IDs.
- Status/account URI values are used transiently for stable hashing or HMAC author pseudonyms. Raw account IDs, account objects, canonical URIs and full provider responses are not copied into normalized comment provenance.
- The endpoint does not expose selectable sorting, so the operation advertises `provider_defined` order instead of simulating new/top semantics.

## Remaining gates

- Run an opt-in live canary against each production-configured instance before scheduling it, because public-preview and rate policies are controlled independently by each operator.
- If an instance requires an app token, add a separately reviewed credential provider rather than weakening target or browser boundaries.

## Primary references

- Hashtag timeline and pagination: https://docs.joinmastodon.org/methods/timelines/#tag
- Account lookup and statuses: https://docs.joinmastodon.org/methods/accounts/#lookup
- Account status listing: https://docs.joinmastodon.org/methods/accounts/#statuses
- Status read and context: https://docs.joinmastodon.org/methods/statuses/#get and https://docs.joinmastodon.org/methods/statuses/#context
- Context entity: https://docs.joinmastodon.org/entities/Context/
- Status entity, URI, reblog, reply and media fields: https://docs.joinmastodon.org/entities/Status/
- API pagination guidance: https://docs.joinmastodon.org/api/guidelines/#pagination
- Instance metadata: https://docs.joinmastodon.org/methods/instance/#v2
