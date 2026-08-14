# Crawler runtime observability boundary

## Typed failures

- Provider and adapter failures use the shared `CrawlerFailure` taxonomy. The run manager persists only the safe message, code, retryability, optional retry delay, selected provider and operation.
- Provider payloads, response bodies, cookies, tokens, author identifiers and exception `details` are not copied into source-run records or browser events.
- Unexpected implementation exceptions remain separate from typed provider outcomes so a programming fault cannot be mislabeled as a provider contract change.

## Parser drift alert

- `PARSE_CHANGED` places the source run in terminal `failed/parser_drift`, persists `error_code=PARSE_CHANGED`, and emits a dedicated `parser-drift-alert` event followed by the normal terminal source-run event.
- The alert contains only batch/source-run/source/provider/operation identity, the typed code, bounded safe message and timestamp. This is sufficient for the dashboard to refresh and display the operation failure without storing the response that triggered it.
- The frontend recognizes the parser-drift phase, displays the typed error code, and treats the alert as a snapshot-refresh trigger. It does not render provider diagnostic payloads.
- A drift alert is evidence to pause/review the affected operation; it is not authority to loosen parsing, copy a vendor fixture, bypass a challenge or silently change a clean-room contract.

## Remaining gate

- The production canary scheduler and notification destination still require an explicit deployment policy. Current alerts are durable in source-run history and live on the run event stream, but no external email/chat notification is sent automatically.
- Any future aggregate alert collection needs a bounded retention policy and must store no provider response bodies.
