"""Social media caption cleaning, spam contact filtering, and title sanitization."""
from __future__ import annotations

import re

# Regex for Chinese mainland mobile numbers (11 digits starting with 13-19)
_RE_PHONE_CN = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")

# Regex for WeChat / QQ / contact strings
_RE_CONTACT = re.compile(
    r"(?:(?:VX|vx|Vx|WX|wx|微信|威信|weixin|wechat|QQ|qq)\s*[:：=]?\s*[A-Za-z0-9_\-]{4,}|"
    r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}|"
    r"(?:https?://|www\.)\S+)",
    flags=re.IGNORECASE,
)

# Mentions: @username
_RE_MENTION = re.compile(r"[@＠][^\s@＠#＃]{1,40}")

# Hashtags: #topic or #topic[话题]#
_RE_DOUYIN_TOPIC = re.compile(r"#([^#\s]+?)\[话题\]#")
_RE_HASHTAG = re.compile(r"[#＃]([^\s#＃]+)")

# Windows illegal filename characters
_RE_WIN_ILLEGAL = re.compile(r'[<>:"/\\|?*\r\n\t]+')


def clean_caption_text(text: str) -> str:
    """Strip spam contact numbers, WeChat/QQ IDs, and user mentions from raw captions."""
    if not text:
        return ""

    s = text

    # Remove Douyin specific topic suffix: #topic[话题]# -> #topic
    s = _RE_DOUYIN_TOPIC.sub(r"#\1", s)

    # Remove contact info before mentions
    s = _RE_CONTACT.sub(" ", s)

    # Remove phone numbers
    s = _RE_PHONE_CN.sub(" ", s)

    # Remove @mentions
    s = _RE_MENTION.sub(" ", s)

    # Clean leading/trailing punctuation noise
    s = re.sub(r"^[\s|｜/\\\-–—·:：,，、。!！?？]+", "", s)
    s = re.sub(r"[\s|｜/\\\-–—·:：,，、]+$", "", s)

    return re.sub(r"\s+", " ", s).strip()


def extract_hashtags(text: str) -> list[str]:
    """Extract list of unique hashtag strings (without leading #) from text."""
    if not text:
        return []

    tags = _RE_HASHTAG.findall(text)
    # Deduplicate while preserving order
    seen = set()
    result = []
    for t in tags:
        t_clean = t.replace("[话题]", "").strip()
        if t_clean and t_clean not in seen:
            seen.add(t_clean)
            result.append(t_clean)
    return result


def truncate_at_word_boundary(text: str, max_chars: int = 60) -> str:
    """Truncate title cleanly at word boundaries, avoiding cut-off words."""
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]
    last_space = max(truncated.rfind(" "), truncated.rfind("-"), truncated.rfind("_"))
    if last_space > int(max_chars * 0.6):
        truncated = truncated[:last_space]

    return truncated.rstrip(" .,!?:;-_")


def sanitize_filename(text: str, max_chars: int = 80) -> str:
    """Produce safe filename for Windows filesystem."""
    cleaned = clean_caption_text(text)
    # Remove # from filename
    cleaned = cleaned.replace("#", "").replace("＃", "")
    safe = _RE_WIN_ILLEGAL.sub("_", cleaned).strip(" ._")
    if not safe:
        safe = "video"
    return truncate_at_word_boundary(safe, max_chars)
