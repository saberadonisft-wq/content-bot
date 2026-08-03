from __future__ import annotations

import math
import re
import unicodedata
from datetime import UTC, datetime
from typing import Any

CJK_PATTERN = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
HIRAGANA_KATAKANA_PATTERN = re.compile(r"[\u3040-\u30ff]")
HAN_PATTERN = re.compile(r"[\u3400-\u9fff]")
HANGUL_PATTERN = re.compile(r"[\uac00-\ud7af]")
VIETNAMESE_PATTERN = re.compile(
    r"[ăâđêôơưáàảãạấầẩẫậắằẳẵặéèẻẽẹếềểễệíìỉĩịóòỏõọốồổỗộớờởỡợúùủũụứừửữựýỳỷỹỵ]",
    re.IGNORECASE,
)

LANGUAGE_LABELS = {
    "vi": "Vietnamese",
    "en": "English",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "und": "Unknown",
}

POSITIVE_TERMS: dict[str, float] = {
    "not bad": 2,
    "không tệ": 2,
    "good": 1,
    "great": 1,
    "amazing": 1,
    "love": 1,
    "fun": 1,
    "excellent": 1,
    "beautiful": 1,
    "masterpiece": 2,
    "improved": 1,
    "smooth": 1,
    "recommend": 1,
    "hay": 1,
    "tốt": 1,
    "tuyệt": 1,
    "thích": 1,
    "mượt": 1,
    "đẹp": 1,
    "đáng chơi": 2,
    "xuất sắc": 2,
    "好玩": 1,
    "优秀": 1,
    "喜欢": 1,
    "流畅": 1,
    "推荐": 1,
}

NEGATIVE_TERMS: dict[str, float] = {
    "not good": 2,
    "không tốt": 2,
    "bad": 1,
    "broken": 1,
    "bug": 1,
    "crash": 2,
    "lag": 1,
    "stutter": 1,
    "boring": 1,
    "hate": 1,
    "awful": 2,
    "terrible": 2,
    "disappointing": 1,
    "refund": 1,
    "pay to win": 2,
    "lỗi": 1,
    "tệ": 1,
    "giật": 1,
    "văng game": 2,
    "chán": 1,
    "thất vọng": 1,
    "hút máu": 2,
    "崩溃": 2,
    "闪退": 2,
    "卡顿": 1,
    "垃圾": 2,
    "失望": 1,
    "氪金": 1,
}

TOPIC_TERMS: dict[str, tuple[str, tuple[str, ...]]] = {
    "bugs": ("Bugs & crashes", ("bug", "crash", "glitch", "broken", "error", "lỗi", "văng game", "崩溃", "闪退", "漏洞")),
    "performance": ("Performance", ("fps", "frame rate", "performance", "optimization", "stutter", "lag", "giật", "khựng", "tối ưu", "卡顿", "帧率", "优化")),
    "gameplay": ("Gameplay", ("gameplay", "combat", "boss", "build", "weapon", "difficulty", "controls", "chơi", "chiến đấu", "vũ khí", "玩法", "战斗", "技能")),
    "updates": ("Updates & content", ("update", "patch", "season", "roadmap", "event", "new content", "bản cập nhật", "bản vá", "sự kiện", "更新", "补丁", "活动", "赛季")),
    "monetization": ("Monetization", ("price", "microtransaction", "battle pass", "pay to win", "gacha", "giá", "nạp", "hút máu", "氪金", "抽卡", "价格")),
    "story": ("Story & lore", ("story", "lore", "ending", "character", "plot", "narrative", "cốt truyện", "nhân vật", "kết thúc", "剧情", "角色", "结局")),
    "community": ("Community", ("community", "mod", "streamer", "esports", "tournament", "fan art", "cộng đồng", "giải đấu", "主播", "电竞", "比赛", "社区")),
}


def normalized(value: str) -> str:
    value = unicodedata.normalize("NFKD", value.casefold())
    value = "".join(char for char in value if not unicodedata.combining(char))
    return re.sub(r"[^\w#]+", " ", value).strip()


def clean_terms(terms: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for term in terms:
        term = term.strip()
        key = normalized(term)
        if term and key and key not in seen:
            seen.add(key)
            cleaned.append(term)
    return cleaned


def contains_term(source: str, term: str) -> bool:
    """Match words/phrases without Latin substring leaks; keep CJK infix matching."""
    if not source or not term:
        return False
    if CJK_PATTERN.search(term):
        return term in source
    return re.search(rf"(?<!\w){re.escape(term)}(?!\w)", source) is not None


def relevance(title: str, body: str, hashtags: list[str], author: str, include_terms: list[str], exclude_terms: list[str]) -> tuple[float, list[str]]:
    title_text = normalized(title)
    body_text = normalized(body)
    hashtag_text = normalized(" ".join(hashtags))
    author_text = normalized(author)
    reasons: list[str] = []
    score = 0.0

    for term in exclude_terms:
        key = normalized(term)
        if key and any(contains_term(source, key) for source in (title_text, body_text, hashtag_text, author_text)):
            return 0.0, [f"excluded by: {term}"]

    for term in include_terms:
        key = normalized(term)
        if not key:
            continue
        if contains_term(title_text, key):
            score += 40
            reasons.append(f"title: {term}")
        elif contains_term(hashtag_text, key):
            score += 25
            reasons.append(f"hashtag: {term}")
        elif contains_term(body_text, key):
            score += 20
            reasons.append(f"content: {term}")
        elif contains_term(author_text, key):
            score += 5
            reasons.append(f"author: {term}")
    return min(score, 100.0), reasons


def engagement(metrics: dict[str, int]) -> int:
    return (
        int(metrics.get("like_count", 0))
        + int(metrics.get("reaction_count", 0))
        + 2 * int(metrics.get("comment_count", 0))
        + 3 * int(metrics.get("share_count", 0))
        + 2 * int(metrics.get("favorite_count", 0))
    )


def recency_score(published_at: datetime | None, now: datetime | None = None) -> float:
    if not published_at:
        return 0.25
    now = now or datetime.now(UTC)
    if published_at.tzinfo is None:
        published_at = published_at.replace(tzinfo=UTC)
    age_hours = max((now - published_at).total_seconds() / 3600, 0)
    return math.exp(-age_hours / 48)


def percentile(values: list[float], value: float) -> float:
    if not values:
        return 0.0
    return sum(candidate <= value for candidate in values) / len(values)


def detect_language(text: str, locale: str | None = None) -> dict[str, Any]:
    """Return a conservative script/locale language hint, never a translation claim."""

    if HIRAGANA_KATAKANA_PATTERN.search(text):
        code, confidence, reason = "ja", 0.98, "Japanese script detected"
    elif HANGUL_PATTERN.search(text):
        code, confidence, reason = "ko", 0.98, "Korean script detected"
    elif HAN_PATTERN.search(text):
        code, confidence, reason = "zh", 0.92, "Han characters detected"
    elif VIETNAMESE_PATTERN.search(text):
        code, confidence, reason = "vi", 0.92, "Vietnamese characters detected"
    else:
        locale_code = (locale or "").lower().replace("_", "-").split("-")[0]
        if locale_code in LANGUAGE_LABELS and locale_code != "und":
            code, confidence, reason = locale_code, 0.85, f"Source locale: {locale}"
        elif re.search(r"[a-zA-Z]", text):
            code, confidence, reason = "en", 0.55, "Latin text; English used as a low-confidence fallback"
        else:
            code, confidence, reason = "und", 0.0, "No supported language signal"
    return {
        "code": code,
        "label": LANGUAGE_LABELS[code],
        "confidence": confidence,
        "reason": reason,
    }


def _matched_terms(text: str, terms: list[str] | tuple[str, ...]) -> list[str]:
    source = normalized(text)
    return [term for term in terms if contains_term(source, normalized(term))]


def content_insights(title: str, body: str, hashtags: list[str], locale: str | None = None) -> dict[str, Any]:
    """Explainable game-domain heuristics; unknown signals remain neutral/empty."""

    text = " ".join(part for part in (title, body, " ".join(hashtags)) if part)
    positive = _matched_terms(text, list(POSITIVE_TERMS))
    negative = _matched_terms(text, list(NEGATIVE_TERMS))
    for phrase, nested in (("not bad", "bad"), ("không tệ", "tệ")):
        if phrase in positive and nested in negative:
            negative.remove(nested)
    for phrase, nested in (("not good", "good"), ("không tốt", "tốt")):
        if phrase in negative and nested in positive:
            positive.remove(nested)
    positive_score = sum(POSITIVE_TERMS[term] for term in positive)
    negative_score = sum(NEGATIVE_TERMS[term] for term in negative)
    total = positive_score + negative_score
    sentiment_score = (positive_score - negative_score) / total if total else 0.0
    if positive and negative and abs(sentiment_score) < 0.5:
        sentiment = "mixed"
    elif sentiment_score > 0:
        sentiment = "positive"
    elif sentiment_score < 0:
        sentiment = "negative"
    else:
        sentiment = "neutral"

    topics = []
    for topic_id, (label, terms) in TOPIC_TERMS.items():
        matches = _matched_terms(text, terms)
        if matches:
            topics.append({"id": topic_id, "label": label, "reasons": matches[:5]})

    sentiment_reasons = [*(f"positive: {term}" for term in positive), *(f"negative: {term}" for term in negative)]
    return {
        "language": detect_language(text, locale),
        "sentiment": {
            "label": sentiment,
            "score": round(sentiment_score, 3),
            "reasons": sentiment_reasons[:8],
        },
        "topics": topics,
        "method": "rule-based-v1",
    }


def insights_match(
    insights: dict[str, Any],
    language: str | None = None,
    sentiment: str | None = None,
    topic: str | None = None,
) -> bool:
    return (
        (not language or insights["language"]["code"] == language)
        and (not sentiment or insights["sentiment"]["label"] == sentiment)
        and (not topic or any(row["id"] == topic for row in insights["topics"]))
    )
