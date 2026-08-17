"""Migration script to copy business data from MongoDB Atlas to Local MongoDB.

Transfers all collections (keywords, content_items, comments, matches, snapshots, batches, runs, metadata)
from the cloud cluster to the local machine so that all content and research data resides exclusively locally.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Add backend directory to sys.path
backend_dir = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(backend_dir))

import certifi
from pymongo import MongoClient, ReplaceOne


def migrate(
    atlas_uri: str,
    local_uri: str = "mongodb://localhost:27017",
    database_name: str = "content_bot",
) -> None:
    print("=" * 60)
    print("Content Bot — Migration: MongoDB Atlas -> Local MongoDB")
    print("=" * 60)

    print(f"\n1. Connecting to Cloud Atlas: {atlas_uri.split('@')[-1] if '@' in atlas_uri else atlas_uri}")
    atlas_client = MongoClient(
        atlas_uri,
        tz_aware=True,
        serverSelectionTimeoutMS=5000,
        tlsCAFile=certifi.where() if atlas_uri.startswith("mongodb+srv://") else None,
    )
    try:
        atlas_client.admin.command("ping")
        print("   [OK] Connected to MongoDB Atlas.")
    except Exception as e:
        print(f"   [ERROR] Cannot connect to MongoDB Atlas: {e}")
        sys.exit(1)

    print(f"\n2. Connecting to Local MongoDB: {local_uri}")
    local_client = MongoClient(
        local_uri,
        tz_aware=True,
        serverSelectionTimeoutMS=5000,
    )
    try:
        local_client.admin.command("ping")
        print("   [OK] Connected to Local MongoDB.")
    except Exception as e:
        print(f"   [ERROR] Cannot connect to Local MongoDB: {e}")
        print("\n   => Ban can cai dat hoac khoi dong MongoDB Community local:")
        print("      winget install MongoDB.Server")
        print("      net start MongoDB")
        sys.exit(1)

    atlas_db = atlas_client[database_name]
    local_db = local_client[database_name]

    collections = atlas_db.list_collection_names()
    if not collections:
        print(f"\n[INFO] Khong tim thay collection nao trong database '{database_name}' tren Atlas.")
        return

    print(f"\n3. Bat dau copy {len(collections)} collections tu Atlas sang Local:")
    total_docs = 0

    for col_name in sorted(collections):
        if col_name.startswith("system."):
            continue

        atlas_col = atlas_db[col_name]
        local_col = local_db[col_name]

        docs = list(atlas_col.find())
        count = len(docs)
        total_docs += count

        if count == 0:
            print(f"   - {col_name:30}: 0 documents")
            continue

        # Bulk upsert to local
        operations = [
            ReplaceOne({"_id": doc["_id"]}, doc, upsert=True)
            for doc in docs
        ]
        local_col.bulk_write(operations, ordered=False)
        print(f"   - {col_name:30}: {count:5} documents -> [COPIED]")

    print(f"\n4. Khoi tao indexes tren Local MongoDB...")
    from app.config import settings
    from app.mongo import MongoStore

    settings.mongodb_uri = local_uri
    settings.mongodb_database = database_name
    store = MongoStore(database=local_db)
    store._available = True
    store.initialize(ping=False)
    print("   [OK] Indexes da duoc khoi tao thanh cong.")

    print("\n" + "=" * 60)
    print(f"MIGRATION COMPLETE! Tong so documents da chuyen: {total_docs}")
    print("Toan bo du lieu nghiep vu gio day da nam an toan tren may local cua ban.")
    print("=" * 60)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate Content Bot data from MongoDB Atlas to Local MongoDB")
    parser.add_argument(
        "--atlas-uri",
        required=True,
        help="MongoDB Atlas connection URI (e.g., mongodb+srv://user:pass@...)",
    )
    parser.add_argument(
        "--local-uri",
        default="mongodb://localhost:27017",
        help="Local MongoDB connection URI (default: mongodb://localhost:27017)",
    )
    parser.add_argument(
        "--database",
        default="content_bot",
        help="Database name to migrate (default: content_bot)",
    )
    args = parser.parse_args()
    migrate(args.atlas_uri, args.local_uri, args.database)
