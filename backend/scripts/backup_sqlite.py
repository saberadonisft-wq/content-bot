"""Create a consistent SQLite backup without importing or starting the application."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from app.services.sqlite_backup import backup_sqlite


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    backup_sqlite(args.source, args.destination)
    print("Backup created and integrity verified.")


if __name__ == "__main__":
    main()
