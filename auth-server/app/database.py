from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from bson import ObjectId
import certifi
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.database import Database

from .config import settings

logger = logging.getLogger("auth_server.database")


class InMemoryCollection:
    """In-memory collection fallback for testing or when MongoDB is not connected."""

    def __init__(self) -> None:
        self._docs: dict[str, dict[str, Any]] = {}

    def find_one(self, filter_dict: dict[str, Any]) -> dict[str, Any] | None:
        for doc in self._docs.values():
            match = True
            for k, v in filter_dict.items():
                if k == "_id" and isinstance(v, (str, ObjectId)):
                    if str(doc.get("_id")) != str(v):
                        match = False
                        break
                elif doc.get(k) != v:
                    match = False
                    break
            if match:
                return dict(doc)
        return None

    def insert_one(self, doc: dict[str, Any]) -> Any:
        new_doc = dict(doc)
        if "_id" not in new_doc:
            new_doc["_id"] = ObjectId()
        self._docs[str(new_doc["_id"])] = new_doc

        class InsertResult:
            inserted_id = new_doc["_id"]

        return InsertResult()

    def update_one(self, filter_dict: dict[str, Any], update_dict: dict[str, Any]) -> Any:
        target = self.find_one(filter_dict)
        if not target:
            return None
        doc_id = str(target["_id"])
        current = self._docs[doc_id]
        if "$set" in update_dict:
            current.update(update_dict["$set"])
        return current

    def delete_one(self, filter_dict: dict[str, Any]) -> Any:
        target = self.find_one(filter_dict)
        if target:
            del self._docs[str(target["_id"])]

    def find(self, filter_dict: dict[str, Any] | None = None, skip: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        results = []
        for doc in self._docs.values():
            if not filter_dict:
                results.append(dict(doc))
                continue
            match = True
            for k, v in filter_dict.items():
                if doc.get(k) != v:
                    match = False
                    break
            if match:
                results.append(dict(doc))
        # sort by created_at desc if available
        results.sort(key=lambda x: x.get("created_at", datetime.min), reverse=True)
        return results[skip : skip + limit]

    def count_documents(self, filter_dict: dict[str, Any] | None = None) -> int:
        if not filter_dict:
            return len(self._docs)
        return len(self.find(filter_dict))

    def create_index(self, *args: Any, **kwargs: Any) -> None:
        pass


class AuthDatabase:
    """Database persistence layer for auth server."""

    def __init__(self) -> None:
        self.client: MongoClient | None = None
        self.db: Database | None = None
        self._users_col: Any = None
        self._tokens_col: Any = None
        self._init_connection()

    def _init_connection(self) -> None:
        if settings.mongodb_uri:
            try:
                uri = settings.mongodb_uri
                ca_file = certifi.where() if "mongodb+srv" in uri or "ssl=true" in uri.lower() else None
                self.client = MongoClient(uri, tlsCAFile=ca_file, serverSelectionTimeoutMS=5000)
                # Verify connection
                self.client.admin.command("ping")
                self.db = self.client[settings.mongodb_database]
                self._users_col = self.db["users"]
                self._tokens_col = self.db["refresh_tokens"]
                self._releases_col = self.db["releases"]

                # Create indexes
                self._users_col.create_index([("email", ASCENDING)], unique=True)
                self._users_col.create_index([("google_id", ASCENDING)], sparse=True)
                self._users_col.create_index([("status", ASCENDING)])
                self._tokens_col.create_index([("token", ASCENDING)], unique=True)
                self._tokens_col.create_index([("expires_at", ASCENDING)], expireAfterSeconds=0)
                self._releases_col.create_index([("version", ASCENDING), ("channel", ASCENDING)], unique=True)
                self._releases_col.create_index([("channel", ASCENDING), ("published_at", DESCENDING)])
                logger.info("Connected to MongoDB successfully.")
                return
            except Exception as e:
                logger.warning(f"Failed to connect to MongoDB ({e}). Falling back to in-memory store.")

        # Fallback to in-memory collection
        self._users_col = InMemoryCollection()
        self._tokens_col = InMemoryCollection()
        self._releases_col = InMemoryCollection()

    # User operations
    def find_user_by_email(self, email: str) -> dict[str, Any] | None:
        return self._users_col.find_one({"email": email.lower().strip()})

    def find_user_by_id(self, user_id: str) -> dict[str, Any] | None:
        try:
            oid = ObjectId(user_id)
            doc = self._users_col.find_one({"_id": oid})
            if doc:
                return doc
        except Exception:
            pass
        return self._users_col.find_one({"_id": user_id})

    def find_user_by_google_id(self, google_id: str) -> dict[str, Any] | None:
        return self._users_col.find_one({"google_id": google_id})

    def create_user(self, user_data: dict[str, Any]) -> dict[str, Any]:
        user_data["email"] = user_data["email"].lower().strip()
        if "created_at" not in user_data:
            user_data["created_at"] = datetime.now(UTC)
        result = self._users_col.insert_one(user_data)
        user_data["_id"] = result.inserted_id
        return user_data

    def update_user(self, user_id: str, updates: dict[str, Any]) -> dict[str, Any] | None:
        filter_dict: dict[str, Any] = {"_id": user_id}
        try:
            filter_dict = {"_id": ObjectId(user_id)}
        except Exception:
            pass
        self._users_col.update_one(filter_dict, {"$set": updates})
        return self.find_user_by_id(user_id)

    def delete_user(self, user_id: str) -> bool:
        filter_dict: dict[str, Any] = {"_id": user_id}
        try:
            filter_dict = {"_id": ObjectId(user_id)}
        except Exception:
            pass
        self._users_col.delete_one(filter_dict)
        return True

    def list_users(self, status: str | None = None, skip: int = 0, limit: int = 100) -> list[dict[str, Any]]:
        filter_dict = {"status": status} if status else {}
        # If real pymongo Collection
        if hasattr(self._users_col, "find") and not isinstance(self._users_col, InMemoryCollection):
            cursor = self._users_col.find(filter_dict).sort([("created_at", DESCENDING)]).skip(skip).limit(limit)
            return list(cursor)
        # In-memory collection fallback
        return self._users_col.find(filter_dict, skip=skip, limit=limit)

    def count_users(self, status: str | None = None) -> int:
        filter_dict = {"status": status} if status else {}
        return self._users_col.count_documents(filter_dict)

    # Token operations
    def store_refresh_token(self, token: str, user_id: str, expires_at: datetime) -> None:
        self._tokens_col.insert_one({
            "token": token,
            "user_id": str(user_id),
            "created_at": datetime.now(UTC),
            "expires_at": expires_at,
        })

    def find_refresh_token(self, token: str) -> dict[str, Any] | None:
        return self._tokens_col.find_one({"token": token})

    def revoke_refresh_token(self, token: str) -> None:
        self._tokens_col.delete_one({"token": token})

    def revoke_all_user_tokens(self, user_id: str) -> None:
        if hasattr(self._tokens_col, "delete_many"):
            self._tokens_col.delete_many({"user_id": str(user_id)})

    # Release operations
    def create_release(self, release_data: dict[str, Any]) -> dict[str, Any]:
        if "published_at" not in release_data:
            release_data["published_at"] = datetime.now(UTC)
        # Remove existing release with same version and channel if present
        self.delete_release(release_data["version"], release_data.get("channel", "stable"))
        result = self._releases_col.insert_one(release_data)
        release_data["_id"] = getattr(result, "inserted_id", release_data.get("_id"))
        return release_data

    def find_release_by_version(self, version: str, channel: str = "stable") -> dict[str, Any] | None:
        return self._releases_col.find_one({"version": version.strip(), "channel": channel})

    def find_release_by_id(self, release_id: str) -> dict[str, Any] | None:
        try:
            oid = ObjectId(release_id)
            doc = self._releases_col.find_one({"_id": oid})
            if doc:
                return doc
        except Exception:
            pass
        return self._releases_col.find_one({"_id": release_id})

    def get_latest_release(self, channel: str = "stable") -> dict[str, Any] | None:
        filter_dict = {"channel": channel}
        if hasattr(self._releases_col, "find") and not isinstance(self._releases_col, InMemoryCollection):
            cursor = self._releases_col.find(filter_dict).sort([("published_at", DESCENDING)]).limit(1)
            docs = list(cursor)
            return docs[0] if docs else None
        docs = self._releases_col.find(filter_dict, skip=0, limit=1)
        return docs[0] if docs else None

    def list_releases(self, channel: str | None = None, skip: int = 0, limit: int = 50) -> list[dict[str, Any]]:
        filter_dict = {"channel": channel} if channel else {}
        if hasattr(self._releases_col, "find") and not isinstance(self._releases_col, InMemoryCollection):
            cursor = self._releases_col.find(filter_dict).sort([("published_at", DESCENDING)]).skip(skip).limit(limit)
            return list(cursor)
        return self._releases_col.find(filter_dict, skip=skip, limit=limit)

    def delete_release(self, version: str, channel: str = "stable") -> bool:
        self._releases_col.delete_one({"version": version.strip(), "channel": channel})
        return True


db = AuthDatabase()

