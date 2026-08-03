from __future__ import annotations

import hashlib
import re
from collections import Counter, defaultdict
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .text import normalized

URL_PATTERN = re.compile(r"https?://[^\s<>()\[\]{}\"']+", re.IGNORECASE)
CJK_RUN = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]+")
LATIN_TOKEN = re.compile(r"[a-z0-9]{3,}")
TRACKING_KEYS = {
    "fbclid",
    "gclid",
    "ref",
    "source",
    "utm_campaign",
    "utm_content",
    "utm_medium",
    "utm_source",
    "utm_term",
}
GENERIC_LINK_SEGMENTS = {
    "app",
    "apps",
    "game",
    "games",
    "home",
    "index",
    "official",
    "product",
    "products",
    "store",
}
STOP_WORDS = {
    "about", "after", "and", "cho", "com", "cua", "da", "dang", "duoc",
    "for", "from", "game", "games", "hay", "into", "khi", "la", "moi", "mot",
    "new", "news", "nhan", "nhung", "official", "recommended", "review", "se", "tai",
    "that", "the", "this", "trailer", "tren", "video", "voi", "with", "you", "your",
}


class DisjointSet:
    def __init__(self, size: int):
        self.parents = list(range(size))

    def find(self, value: int) -> int:
        while self.parents[value] != value:
            self.parents[value] = self.parents[self.parents[value]]
            value = self.parents[value]
        return value

    def union(self, left: int, right: int) -> None:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            self.parents[right_root] = left_root


def _clean_url(value: str) -> str:
    value = value.rstrip(".,;:!?)]}")
    parsed = urlsplit(value)
    host = parsed.netloc.casefold().removeprefix("www.")
    path = re.sub(r"/+", "/", parsed.path).rstrip("/") or "/"
    query = urlencode(
        sorted(
            (key, item)
            for key, item in parse_qsl(parsed.query, keep_blank_values=True)
            if key.casefold() not in TRACKING_KEYS and not key.casefold().startswith("utm_")
        )
    )
    return urlunsplit(("https", host, path, query, "")) if host else ""


def _is_specific_story_link(value: str) -> bool:
    """Reject homepages, product pages and short generic paths as story evidence."""
    path_parts = [part.casefold() for part in urlsplit(value).path.split("/") if part]
    if not path_parts:
        return False
    if path_parts[0] in GENERIC_LINK_SEGMENTS:
        return False
    if len(path_parts) > 1:
        return True
    segment = path_parts[0]
    return len(segment) >= 12 and ("-" in segment or "_" in segment)


def _links(item: dict[str, Any]) -> set[str]:
    text = " ".join((item.get("title") or "", item.get("body_snippet") or ""))
    return {
        cleaned
        for match in URL_PATTERN.findall(text)
        if (cleaned := _clean_url(match)) and _is_specific_story_link(cleaned)
    }


def _remove_ignored(value: str, ignore_terms: list[str]) -> str:
    result = f" {normalized(value)} "
    ignored = {normalized(term) for term in ignore_terms}
    for term in sorted((term for term in ignored if term), key=len, reverse=True):
        result = result.replace(f" {term} ", " ")
        if " " not in term:
            result = result.replace(term, " ")
    return re.sub(r"\s+", " ", result).strip()


def _title_tokens(item: dict[str, Any], ignore_terms: list[str]) -> set[str]:
    value = _remove_ignored(item.get("title") or "", ignore_terms)
    tokens = {token for token in LATIN_TOKEN.findall(value) if token not in STOP_WORDS}
    for run in CJK_RUN.findall(value):
        if len(run) == 2:
            tokens.add(f"cjk:{run}")
        elif len(run) > 2:
            tokens.update(f"cjk:{run[index:index + 2]}" for index in range(len(run) - 1))
    return tokens


def _item_host(item: dict[str, Any]) -> str:
    host = urlsplit(item.get("canonical_url") or "").netloc.casefold().removeprefix("www.")
    return host or item.get("source_id") or "unknown"


def _origin(item: dict[str, Any]) -> str:
    source_id = item.get("source_id") or "unknown"
    return f"{source_id}:{_item_host(item)}" if source_id == "web" else source_id


def _candidate(item: dict[str, Any], ignore_terms: list[str]) -> dict[str, Any]:
    return {
        "item": item,
        "links": _links(item),
        "origin": _origin(item),
        "tokens": _title_tokens(item, ignore_terms),
    }


def _match(left: dict[str, Any], right: dict[str, Any]) -> str | None:
    shared_links = left["links"] & right["links"]
    if shared_links:
        host = urlsplit(min(shared_links)).netloc
        return f"shared specific link: {host}"
    if left["origin"] == right["origin"]:
        return None

    left_tokens = left["tokens"]
    right_tokens = right["tokens"]
    if min(len(left_tokens), len(right_tokens)) < 4:
        return None
    shared = left_tokens & right_tokens
    if len(shared) < 3:
        return None
    union = left_tokens | right_tokens
    jaccard = len(shared) / len(union)
    containment = len(shared) / min(len(left_tokens), len(right_tokens))
    if jaccard < 0.55 and containment < 0.75:
        return None
    terms = ", ".join(sorted(shared)[:5]).replace("cjk:", "")
    return f"shared title terms: {terms}"


def _item_payload(item: dict[str, Any]) -> dict[str, Any]:
    insights = item["insights"]
    return {
        "id": item["id"],
        "title": item["title"],
        "source_id": item["source_id"],
        "item_host": _item_host(item),
        "canonical_url": item["canonical_url"],
        "trend_score": item["trend_score"],
        "published_at": item.get("published_at"),
        "language": insights["language"]["code"],
        "sentiment": insights["sentiment"]["label"],
        "topics": [topic["label"] for topic in insights["topics"]],
    }


def cluster_items(
    items: list[dict[str, Any]],
    ignore_terms: list[str],
    min_items: int = 2,
    limit: int = 20,
) -> dict[str, Any]:
    """Build conservative, explainable clusters from already-filtered item payloads.

    Candidate discovery uses inverted link/token indexes, then evaluates only pairs
    sharing a strict evidence precursor. This avoids comparing every social post to
    every other post as a keyword collection grows.
    """

    ordered = sorted(items, key=lambda item: (-float(item["trend_score"]), int(item["id"])))
    candidates = [_candidate(item, ignore_terms) for item in ordered]
    groups = DisjointSet(len(candidates))
    link_index: dict[str, list[int]] = defaultdict(list)
    token_index: dict[str, list[int]] = defaultdict(list)
    edges: list[tuple[int, int, str]] = []

    for index, candidate in enumerate(candidates):
        possible: set[int] = set()
        for link in candidate["links"]:
            possible.update(link_index[link])

        token_hits: Counter[int] = Counter()
        for token in candidate["tokens"]:
            token_hits.update(token_index[token])
        possible.update(other for other, count in token_hits.items() if count >= 3)

        for other in sorted(possible):
            reason = _match(candidates[other], candidate)
            if reason:
                groups.union(other, index)
                edges.append((other, index, reason))

        for link in candidate["links"]:
            link_index[link].append(index)
        for token in candidate["tokens"]:
            token_index[token].append(index)

    grouped_candidates: dict[int, list[int]] = defaultdict(list)
    for index in range(len(candidates)):
        grouped_candidates[groups.find(index)].append(index)
    group_reasons: dict[int, set[str]] = defaultdict(set)
    for left, right, reason in edges:
        if groups.find(left) == groups.find(right):
            group_reasons[groups.find(left)].add(reason)

    clusters: list[dict[str, Any]] = []
    for root, member_indexes in grouped_candidates.items():
        if len(member_indexes) < min_items:
            continue
        members = [ordered[index] for index in member_indexes]
        sentiments = Counter(item["insights"]["sentiment"]["label"] for item in members)
        topics = Counter(
            topic["label"]
            for item in members
            for topic in item["insights"]["topics"]
        )
        source_ids = sorted({item["source_id"] for item in members})
        item_hosts = sorted({_item_host(item) for item in members})
        origins = sorted({_origin(item) for item in members})
        scores = [float(item["trend_score"]) for item in members]
        member_ids = sorted(int(item["id"]) for item in members)
        cluster_id = hashlib.blake2s(
            ",".join(map(str, member_ids)).encode(), digest_size=6
        ).hexdigest()
        latest_at = max(
            (item.get("published_at") for item in members if item.get("published_at")),
            default=None,
        )
        clusters.append(
            {
                "id": cluster_id,
                "label": members[0]["title"],
                "item_count": len(members),
                "source_ids": source_ids,
                "item_hosts": item_hosts,
                "origin_count": len(origins),
                "max_trend_score": round(max(scores), 1),
                "average_trend_score": round(sum(scores) / len(scores), 1),
                "latest_at": latest_at,
                "sentiments": dict(sorted(sentiments.items())),
                "topics": [
                    {"label": label, "count": count}
                    for label, count in sorted(topics.items(), key=lambda row: (-row[1], row[0]))[:5]
                ],
                "match_reasons": sorted(group_reasons[root]),
                "items": [_item_payload(item) for item in members],
            }
        )

    clusters.sort(
        key=lambda cluster: (
            -cluster["origin_count"],
            -cluster["item_count"],
            -cluster["max_trend_score"],
            cluster["id"],
        )
    )
    selected = clusters[:limit]
    return {
        "total_items": len(items),
        "clustered_items": sum(cluster["item_count"] for cluster in clusters),
        "cluster_count": len(clusters),
        "returned_clustered_items": sum(cluster["item_count"] for cluster in selected),
        "returned_cluster_count": len(selected),
        "truncated": len(selected) < len(clusters),
        "clusters": selected,
        "method": "conservative-title-link-v2",
        "caveat": "Clusters are discovery aids. They require a shared specific article link or strong title-term overlap across distinct origins; review members before treating them as one story.",
    }
