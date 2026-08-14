"""Shared Steam public-review contracts and bounded selection policy."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse


@dataclass(slots=True)
class SteamRequestBudget:
    discovery_limit: int
    review_limit: int
    total_limit: int | None = None
    discovery_requests: int = 0
    review_requests: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.discovery_limit <= 20 or not 1 <= self.review_limit <= 100:
            raise ValueError("Steam request budgets are invalid")
        if self.total_limit is not None and not 1 <= self.total_limit <= 120:
            raise ValueError("Steam total request budget is invalid")

    def spend_discovery(self) -> bool:
        if self._total_exhausted():
            return False
        if self.discovery_requests >= self.discovery_limit:
            return False
        self.discovery_requests += 1
        return True

    def spend_review(self) -> bool:
        if self._total_exhausted():
            return False
        if self.review_requests >= self.review_limit:
            return False
        self.review_requests += 1
        return True

    def _total_exhausted(self) -> bool:
        return bool(
            self.total_limit is not None
            and self.discovery_requests + self.review_requests >= self.total_limit
        )


def parse_steam_app_id(url: str) -> str:
    parsed = urlparse(url)
    match = re.search(r"/(?:app|games?)/(\d+)(?:/|$)", parsed.path, re.IGNORECASE)
    candidate = match.group(1) if match else str(next(iter(parse_qs(parsed.query).get("appid") or []), ""))
    if not candidate.isdigit() or not 1 <= int(candidate) <= 2_147_483_647:
        raise ValueError("Steam target must contain a valid numeric App ID")
    return str(int(candidate))


def canonical_steam_app_url(app_id: str) -> str:
    normalized = parse_steam_app_id(f"https://store.steampowered.com/app/{app_id}/")
    return f"https://store.steampowered.com/app/{normalized}/"


def select_discovered_apps(
    search_term: str,
    candidates: list[dict[str, Any]],
    *,
    limit: int,
) -> tuple[list[dict[str, Any]], bool]:
    """Prefer exact title matches and reject tied low-confidence top results."""
    normalized_term = _normalized_title(search_term)
    scored: list[tuple[tuple[int, float, int], dict[str, Any]]] = []
    for position, candidate in enumerate(candidates):
        app_id = str(candidate.get("id") or "")
        name = str(candidate.get("name") or "").strip()
        if not app_id.isdigit() or not name:
            continue
        normalized_name = _normalized_title(name)
        exact = int(normalized_name == normalized_term)
        term_tokens = set(normalized_term.split())
        name_tokens = set(normalized_name.split())
        overlap = len(term_tokens & name_tokens) / max(len(term_tokens), 1)
        edition_penalty = int(
            any(marker in name_tokens - term_tokens for marker in ("demo", "dlc", "soundtrack", "server", "test"))
        )
        scored.append(((exact, overlap, -edition_penalty), {**candidate, "_position": position}))
    scored.sort(key=lambda row: (row[0], -row[1]["_position"]), reverse=True)
    if not scored:
        return [], False
    top_score = scored[0][0]
    exact_matches = [row for row in scored if row[0][0] == 1]
    if exact_matches:
        if len(exact_matches) > 1:
            return [], True
        candidate = exact_matches[0][1]
        return [
            {key: value for key, value in candidate.items() if key != "_position"}
        ], False
    tied = [row for row in scored if row[0] == top_score]
    ambiguous = len(tied) > 1
    if top_score[1] < 0.5:
        return [], True
    if ambiguous:
        return [], True
    return [
        {key: value for key, value in candidate.items() if key != "_position"}
        for _score, candidate in scored[: max(1, limit)]
    ], False


def steam_review_payload(
    review: dict[str, Any],
    *,
    app_id: str,
    game_name: str,
) -> dict[str, Any]:
    """Return the exact non-identity allowlist shared by both scan paths."""
    return {
        "provider_id": "steam_public_reviews",
        "app_id": app_id,
        "game_name": game_name[:300],
        "recommendation_id": str(review.get("recommendationid") or ""),
        "language": str(review.get("language") or ""),
        "review": str(review.get("review") or "")[:4_000],
        "timestamp_created": int(review.get("timestamp_created", 0) or 0),
        "timestamp_updated": int(review.get("timestamp_updated", 0) or 0),
        "voted_up": bool(review.get("voted_up")),
        "helpful_votes": int(review.get("votes_up", 0) or 0),
        "funny_votes": int(review.get("votes_funny", 0) or 0),
        "weighted_vote_score": str(review.get("weighted_vote_score") or "")[:100],
        "comment_count": int(review.get("comment_count", 0) or 0),
        "received_for_free": bool(review.get("received_for_free")),
        "written_during_early_access": bool(review.get("written_during_early_access")),
        "primarily_steam_deck": bool(review.get("primarily_steam_deck")),
    }


def steam_review_external_id(app_id: str, review: dict[str, Any]) -> str:
    return app_id + ":" + str(review.get("recommendationid") or "")


def _normalized_title(value: str) -> str:
    return " ".join(re.findall(r"[\w]+", value.casefold(), flags=re.UNICODE))
