"""Mongo item projection and a single aggregation for filtered count and page."""

from __future__ import annotations

import re

from .services.item_query import (
    ANALYSIS_VERSION,
    ItemQuery,
    analyze_item,
    search_text,
    sort_timestamp,
)

PROJECTION_FIELD = "_item_query"
INPUT_FIELDS = ("title", "body_snippet", "hashtags", "locale", "author", "published_at", "first_seen_at")


def projection(item: dict) -> dict:
    return {"version": ANALYSIS_VERSION, "analysis": analyze_item(item),
            "search_text": search_text(item), "sort_at": sort_timestamp(item)}


def unchanged_input(item: dict) -> dict:
    # Comparing the actual inputs also works for legacy rows without a revision.
    return {"_id": item["_id"], **{
        field: item.get(field, {"$exists": False}) for field in INPUT_FIELDS
    }}


def refresh_analysis(store):
    stale = {f"{PROJECTION_FIELD}.version": {"$ne": ANALYSIS_VERSION}}
    while True:
        rows = list(store.db.content_items.find(stale).limit(128))
        if not rows:
            return
        for item in rows:
            store.db.content_items.update_one(unchanged_input(item), {"$set": {PROJECTION_FIELD: projection(item)}})


def select_items(store, query: ItemQuery, limit: int | None, offset: int):
    if not store.is_available:
        return 0, []
    refresh_analysis(store)
    match = {"keyword_id": query.keyword_id, "relevance_score": {"$gt": 0}}
    if query.min_relevance > 0:
        match["relevance_score"]["$gte"] = query.min_relevance
    if query.session_id:
        match["session_id"] = query.session_id
    item_filters = {}
    for value, field in ((query.source_id, "item.source_id"),
                         (query.language, f"item.{PROJECTION_FIELD}.analysis.language.code"),
                         (query.sentiment, f"item.{PROJECTION_FIELD}.analysis.sentiment.label"),
                         (query.topic, f"item.{PROJECTION_FIELD}.analysis.topics.id")):
        if value:
            item_filters[field] = value
    if query.needle:
        item_filters[f"item.{PROJECTION_FIELD}.search_text"] = {"$regex": re.escape(query.needle)}
    order = {"$sort": {"_sort_trend": -1, f"item.{PROJECTION_FIELD}.sort_at": -1, "item._id": 1}}
    page = [order, {"$skip": offset}]
    if limit is not None:
        if limit == 0:
            page.append({"$match": {"_id": {"$exists": False}}})
        else:
            page.append({"$limit": limit})
    pipeline = [
        {"$match": match},
        {"$lookup": {"from": "content_items", "localField": "content_item_id", "foreignField": "_id", "as": "item"}},
        {"$unwind": "$item"}, {"$match": item_filters},
        {"$addFields": {"_sort_trend": {"$ifNull": ["$trend_score", 0]}}},
    ]
    if limit is None:
        # Exports/aggregates may exceed Mongo's per-document limit. A cursor
        # avoids collecting an unbounded result inside a single $facet document.
        matches = store.db.item_keyword_matches.aggregate([*pipeline, order], allowDiskUse=True)
        total = 0
    else:
        pipeline.append({"$facet": {"total": [{"$count": "value"}], "rows": page}})
        result = next(iter(store.db.item_keyword_matches.aggregate(pipeline)), {"total": [], "rows": []})
        matches = result["rows"]
        total = result["total"][0]["value"] if result["total"] else 0
    rows = []
    for match_row in matches:
        if limit is None:
            total += 1
            if total <= offset:
                continue
        item = match_row.pop("item")
        match_row.pop("_sort_trend", None)
        analysis = item[PROJECTION_FIELD]["analysis"]
        rows.append((store.public(item), store.public(match_row), analysis))
    return total, rows
