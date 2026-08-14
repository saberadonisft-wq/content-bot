# Game news RSS/Atom provider provenance

## Provider boundary

- The `web` source is a validated RSS/Atom reader, not an arbitrary website crawler.
- The bundled fallback is a public Google News RSS query feed. It is an aggregator without an API SLA and is not treated as proof that an article URL is the publisher's canonical URL.
- Custom templates are explicit administrator allowlist entries. Only HTTPS is accepted; local/private literal hosts, userinfo, unsupported placeholders, arbitrary redirect hosts, oversized bodies, HTML, and non-feed XML are rejected.
- No browser cookies, anti-bot logic, MediaCrawler code, or undocumented Google News decoding is used.

## Implemented behavior

- Configuration accepts the legacy comma format plus newline-separated or JSON-array templates. JSON is recommended when a URL itself contains a comma. The only template field is `{query}`, expanded with URL encoding.
- Redirects are handled manually with a three-hop cap and may land only on another explicitly configured feed host. Automatic HTTP redirect following is disabled.
- RSS/Atom identity, XML media type/sniffing, two-megabyte response cap, RFC 822/3339/ISO timestamps, HTML-to-text summaries, Atom authors/links, RSS publisher provenance, and enclosure/thumbnail metadata are normalized through bounded allowlists.
- ETag and Last-Modified validators are checkpointed per template digest and normalized search term only after the complete feed document was consumed. Truncated documents are fetched again so an item cap cannot silently acknowledge unseen entries.
- A failed configured feed emits a visible run warning while other valid feeds continue. The source run completes with phase `completed_with_warnings`; it fails with a typed error only when no configured feed produced a valid document or `304` response.
- Saved URLs are scannable only when their host is in configured feed templates and the fetched response independently validates as RSS/Atom.

## Remaining limitation

- Google News aggregator article links remain aggregator links unless the feed itself supplies a safe publisher article URL. Content Bot records publisher name/homepage provenance but deliberately does not reverse-engineer Google News redirect payloads or fetch arbitrary publisher destinations to guess a canonical URL.

## References

- RSS 2.0 specification: https://www.rssboard.org/rss-specification
- Atom Syndication Format (RFC 4287): https://www.rfc-editor.org/rfc/rfc4287
- HTTP conditional requests (RFC 9110): https://www.rfc-editor.org/rfc/rfc9110
