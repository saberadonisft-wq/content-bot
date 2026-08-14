# Crawler provider rollback drill

`backend/scripts/crawler_rollback_drill.py` records the non-mutating rollback state that must exist before the temporary MediaCrawler bridge can enter its final rollback window.

Run from `backend/` while the legacy runtime is still retained:

```powershell
$env:PYTHONPATH = (Get-Location).Path
.\.venv\Scripts\python.exe scripts\crawler_rollback_drill.py
```

The report is atomically retained at `CONTENT_BOT_DATA_DIR/cbce-audits/rollback-drill.json`. It verifies all seven former bridge sources still have an implemented legacy search binding, required bridge/vendor artifacts exist, and CBCE profile v2 is distinct from the legacy profile root. It snapshots only a digest of current rollout configuration and records the explicit rollback settings:

```text
CONTENT_BOT_CBCE_ENABLED=false
CONTENT_BOT_CBCE_PROVIDER_OVERRIDES={}
```

The drill does not open a browser or database and performs no data/profile mutation. Canonical source IDs and shared Mongo entities do not change when switching providers, so provider rollback needs no content-data rewrite. Profile migration/deletion is deliberately outside this drill and must never be inferred from provider selection.

The cutover auditor requires the retained report to match the current crawler project-tree digest. Once all source canaries pass, rollback consists of disabling CBCE and restarting the backend during the declared window. Vendor/profile deletion is a later reviewed action only after that window closes; after deletion the legacy rollback path intentionally ceases to exist.

Current 2026-08-13 evidence: seven legacy bindings, all three required legacy artifacts and separated profile namespaces verified; no browser/database opened and no destructive action performed. The retained drill passed.
