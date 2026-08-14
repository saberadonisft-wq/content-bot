# Startup refactor baseline

Measured on 2026-08-12 before the startup refactor, on the existing Windows development machine.

| Metric | Before | After |
| --- | ---: | ---: |
| Python `import app.main` | 2,923 ms | 1,134 ms |
| Backend cold ready | Not isolated | 3,297 ms |
| Cached `/ready` | 74–472 ms | 2–14 ms |
| `/health` | 3–530 ms | About 4 ms |
| Initial frontend JavaScript | 328.66 kB / 99.28 kB gzip | 242.43 kB / 75.55 kB gzip |
| Initial frontend CSS | 86.35 kB / 16.15 kB gzip | 53.02 kB / 10.80 kB gzip |
| Dependency files excluded from editor indexing | 0 | More than 38,000 |
| Runtime layout | Hidden backend, blocking frontend start | Two parallel foreground VS Code terminals |

The MongoDB safety backup created before legacy cleanup is stored under the ignored `data/backups/` directory. User videos, browser profiles, credentials, models, and the legacy SQLite data file were not deleted.

Acceptance commands:

```powershell
backend\.venv\Scripts\python.exe -m pytest -q
cd frontend
npm run test
npm run lint
npm run build
```
