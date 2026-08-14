# Phase 11 crawler cutover audit

`backend/scripts/crawler_cutover_audit.py` is a read-only fail-closed audit. It
does not change provider flags, stop workers, delete profiles, remove the vendor
submodule or edit configuration.

Run it from `backend/`:

```powershell
.\.venv\Scripts\python.exe scripts\crawler_cutover_audit.py
```

Exit code `0` means both the per-source cutover gates and legacy-runtime cleanup
are complete. Exit code `1` means at least one blocker remains. The JSON report
uses schema `cbce.cutover-audit.v1` and always declares
`destructive_actions_performed=false`.

For every canonical source, the audit selects its minimum committed operation
(official X search is selected instead of its public embed), then checks:

- an implemented non-legacy provider/handler exists;
- the operation's currently resolved provider is not `legacy_only`;
- two safe `passed` canary reports for the same source/provider/operation exist
  at least one hour apart;
- the source's clean-room provenance document exists.

When exactly one valid canary exists, each source gate reports
`next_canary_eligible_at` and `canary_wait_seconds`. A run created earlier than
that instant remains useful operationally but cannot satisfy the separation
gate; the auditor never rounds the interval down.

Evidence is unique by `canary_id`; copying one report to another filename does
not create a second cycle. Reports completed more than five minutes in the
future are ignored, preventing a future timestamp from satisfying the elapsed
interval early.

Each source gate also includes only the latest typed canary state, error code
and completion time for its candidate provider/operation. Safe-message text and
provider diagnostics are deliberately excluded. This distinguishes, for
example, X `PAYMENT_OR_ACCESS_REQUIRED` from a source that has never run while
keeping the report aggregate-only.

The global cutover gate also requires a retained, passing clean-room similarity
report whose project-tree digest still matches the current crawler
implementation. Generate it with `crawler_cleanroom_audit.py` while the vendor
snapshot is still present; details and limitations are documented in
`CLEANROOM_AUDIT.md`.

A retained rollback drill is also mandatory. It proves the seven legacy
bindings/artifacts and separate profile namespaces existed before the rollback
window without opening a browser/database or mutating content. See
`ROLLBACK_DRILL.md`.

Canary evidence is loaded only from `data/cbce-canary-reports/*.json`. A report
is ignored if it does not use `cbce.live-canary.v1`, is not `persisted=false`,
lacks a valid one-way input digest, or contains keys for query/target/content,
provider payloads, IDs, cookies or tokens. Thus manually fabricated raw capture
files cannot accidentally become accepted cutover evidence.

The audit separately lists known legacy runtime artifacts such as `.gitmodules`,
`vendor/mediacrawler`, the bridge scripts and readiness marker. Their presence
does not authorize deletion; cleanup may happen only after `cutover_ready=true`,
the rollback window and backup drill are complete, and the user-owned profile
migration decision is recorded. The deletion must remain a separate reviewed
change.

The audit is intentionally stricter than unit-test success. Missing external
credentials, approval, authenticated browser observations or live canaries stay
visible as blockers rather than being converted into a ready state.
