# Clean-room similarity audit

`backend/scripts/crawler_cleanroom_audit.py` is a read-only, aggregate-only heuristic gate used before MediaCrawler removal. It compares project crawler implementation files with the retained vendor snapshot while the snapshot still exists.

Run from `backend/`:

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe scripts\crawler_cleanroom_audit.py --output cleanroom-similarity.json
```

The retained report is written atomically to `CONTENT_BOT_DATA_DIR/cbce-audits/cleanroom-similarity.json`. `--output` accepts only one direct `.json` filename; it cannot write outside that directory.

The scanner fingerprints normalized 20-token windows and hashes long normalized lines. It suppresses expressions common to many files, then fails on exact file equality, substantial project-side containment, or several exact long-line matches. The report contains only file paths, counts, digests, thresholds and verdicts—never source fragments, literals, credentials or content records.

This is a regression detector, not a legal opinion and not proof that no derivation occurred. A passing result must be combined with the per-provider provenance ledger, independently captured fixtures/contracts, dependency-license review and manual review of suspicious design similarity. Any threshold change invalidates earlier evidence and requires a new review.

The retained vendor digest allows the report to survive the later reviewed vendor-removal change. The cutover auditor also recomputes the current project-tree digest, so any crawler implementation edit makes the retained report stale until the audit is rerun while the vendor snapshot is still available.

Current 2026-08-13 evidence: 117 project implementation files and 211 vendor source files scanned, 20 candidate pairs examined, zero suspicious pairs, no source fragments emitted. The current cutover audit validates the retained project digest and reports `cleanroom_audit.ready=true`.
