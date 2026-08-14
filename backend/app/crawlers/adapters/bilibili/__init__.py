"""Bilibili clean-room target contracts.

No endpoint, selector, signing algorithm or vendor fixture belongs in this
package without an independently recorded provenance entry.
"""

from .comment_provider import (
    BilibiliChildCommentCursor,
    BilibiliChildCommentsAdapter,
    BilibiliComment,
    BilibiliCommentCursor,
    BilibiliCommentPage,
    BilibiliCommentsAdapter,
    BilibiliDomCommentsProvider,
    parse_bilibili_comment,
)
from .creator_provider import BilibiliCreatorCursor, BilibiliDomCreatorProvider
from .dom_provider import (
    BilibiliDomCursor,
    BilibiliDomDetailProvider,
    BilibiliDomSearchProvider,
    parse_compact_count,
    parse_public_datetime,
)
from .provider import (
    BilibiliCreatorAdapter,
    BilibiliCreatorProvider,
    BilibiliDetailAdapter,
    BilibiliDetailProvider,
    BilibiliSearchAdapter,
    BilibiliSearchProvider,
    BilibiliVideo,
    BilibiliVideoPage,
)
from .targets import BilibiliTarget, BilibiliTargetKind, parse_bilibili_target

__all__ = [
    "BilibiliChildCommentCursor",
    "BilibiliChildCommentsAdapter",
    "BilibiliComment",
    "BilibiliCommentCursor",
    "BilibiliCommentPage",
    "BilibiliCommentsAdapter",
    "BilibiliCreatorAdapter",
    "BilibiliCreatorCursor",
    "BilibiliCreatorProvider",
    "BilibiliDetailAdapter",
    "BilibiliDetailProvider",
    "BilibiliDomCommentsProvider",
    "BilibiliDomCreatorProvider",
    "BilibiliDomCursor",
    "BilibiliDomDetailProvider",
    "BilibiliDomSearchProvider",
    "BilibiliSearchAdapter",
    "BilibiliSearchProvider",
    "BilibiliTarget",
    "BilibiliTargetKind",
    "BilibiliVideo",
    "BilibiliVideoPage",
    "parse_bilibili_comment",
    "parse_bilibili_target",
    "parse_compact_count",
    "parse_public_datetime",
]
