# Live crawler canary boundary

## Purpose

`backend/scripts/crawler_canary.py` is the manual evidence runner used before an
operation/provider cutover. It executes an already registered handler with a
tiny item, request and wall-clock budget. It does not import the application
store, create a batch/source-run, update checkpoints or persist content.

The runner is not a scheduler and is not a way to enable a planned provider.
The registry must declare the operation implemented, bind a real handler and
the provider's local health probe must report `ready`.

## Safety invariants

- Network access is refused unless the operator supplies `--live`.
- Search text and target URL arrive as JSON on stdin, never as process-list
  arguments. Credentials continue to come only from the approved application
  configuration or encrypted provider vault.
- `max_items` is limited to 5, `max_requests` to 10 and the deadline to 300
  seconds. The outer deadline cancels the iterator and closes owned resources.
- MediaCrawler/legacy providers require a second explicit
  `allow_legacy=true`; this exists only to collect migration evidence and does
  not satisfy a clean-room cutover gate.
- Challenge, CAPTCHA and permission failures are not bypassed. Typed auth,
  permission, payment/access, rate-limit and parser-drift failures stop the
  canary.
- The report contains aggregate counts, field completeness, metric-key names,
  time bounds, warning/error codes and a one-way input digest. It never contains
  content text, title, author, canonical URL, external ID, query, target URL,
  cookie, token or provider response. `persisted` is always `false`.
- Unexpected exceptions become `UNEXPECTED_FAILURE`; their text is not copied
  to the report.

## Manual usage

Run from `backend/` with a deliberately small, allowed public test target:

```powershell
'{"source_id":"bluesky","operation":"search","query":"gamedev","max_items":2,"max_requests":2,"deadline_seconds":30}' |
  .\.venv\Scripts\python.exe scripts\crawler_canary.py --live
```

For a configured saved-channel surface:

```powershell
'{"source_id":"steam","operation":"scan_channel","target_url":"https://store.steampowered.com/app/570/","max_items":2,"max_requests":2,"deadline_seconds":30}' |
  .\.venv\Scripts\python.exe scripts\crawler_canary.py --live
```

To retain aggregate evidence, `--output steam-2026-08-13-a.json` writes by
atomic replace only beneath the configured `CONTENT_BOT_DATA_DIR/cbce-canary-reports/`
(the default is `backend/data/cbce-canary-reports/` when commands run from
`backend/`). Absolute paths, nested
paths and non-JSON names are rejected. The same aggregate report is written to
stdout.

## Interpreting a report

- `passed` means the registered operation completed under this canary's budget;
  it is one evidence point, not proof of complete coverage.
- `skipped` means the provider, permission, operation binding or explicit
  legacy opt-in was unavailable. It must not be recorded as a passing canary.
- `failed` with `PARSE_CHANGED`, `RATE_LIMITED`, `AUTH_REQUIRED`,
  `PERMISSION_REQUIRED` or `PAYMENT_OR_ACCESS_REQUIRED` blocks cutover until the
  corresponding contract/access review is resolved.
- `timed_out` blocks cutover and requires cleanup/orphan-process verification.

Each provider cutover still requires the operation-specific acceptance matrix,
two canaries at different times where the plan requires them, provenance review
and proof that owned browser/process resources closed. External approvals and
test assets are never inferred from an implementation-only canary.
