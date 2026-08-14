# Shared crawler media downloader boundary

Status: internal runtime primitive implemented, 2026-08-13. No source enables media-byte download merely because this primitive exists.

## Security and storage contract

- Each adapter must supply an explicit DNS host allowlist and source ID. Only HTTPS, default port 443, no userinfo, no fragment, and no private/local literal address are accepted.
- Redirects are manual, bounded, and revalidated against the same allowlist at every hop. Automatic cross-host redirect following is disabled.
- Only a small passive format allowlist is stored: JPEG, PNG, GIF, WebP, AVIF, MP4, WebM, MP3, M4A, and Ogg. Active formats such as SVG and HTML are rejected even when their broad media prefix would otherwise match.
- Declared Content-Length is checked before streaming and every chunk is checked against per-file and per-run byte budgets. File count, total bytes, redirects, timeout, retry attempts, and chunk size are bounded.
- Downloads use a random same-directory `.part` file, flush and fsync, then atomic replace. Failure or cancellation removes the partial file; it cannot publish a truncated final artifact.
- The artifact filename is a digest of the request URL. Public artifact metadata drops the query string so signed URLs/tokens are not persisted or logged.
- Repeated identical URLs reuse the in-run artifact without another request. Different URLs with the same content hash reuse one stored file.

## Integration rule

- A source manifest remains `planned` for `media_download` until that adapter declares its media hosts, MIME policy, product retention boundary, user-visible quota, and source-specific acceptance tests.
- Media metadata already present in content records does not authorize a byte download. The operation must be explicitly requested and budgeted.
- New downloaded collections must be registered in crawler retention/deletion before rollout.

## Acceptance evidence

- Deterministic tests cover unsafe scheme/userinfo/port/private literals, hostile redirects, content type, active SVG rejection, declared and streaming overflow, atomic cleanup, cancellation, transient retry, query redaction, file/byte budgets, duplicate URL, and content-hash dedupe.
