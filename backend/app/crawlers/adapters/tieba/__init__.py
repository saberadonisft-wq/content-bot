"""Baidu Tieba clean-room adapter contracts."""

from .comment_provider import (
    TiebaComment,
    TiebaCommentCursor,
    TiebaCommentPage,
    TiebaCommentsAdapter,
    TiebaCommentsProvider,
    TiebaDomCommentsProvider,
)
from .dom_provider import (
    TIEBA_SEARCH_DOM_CONTRACT,
    TiebaDomContract,
    TiebaDomSearchProvider,
)
from .provider import (
    TiebaDetailAdapter,
    TiebaDetailProvider,
    TiebaSearchAdapter,
    TiebaSearchCursor,
    TiebaSearchProvider,
    TiebaTargetThreadsAdapter,
    TiebaTargetThreadsProvider,
    TiebaThread,
    TiebaThreadPage,
    normalize_tieba_thread,
)
from .target_provider import (
    TIEBA_CREATOR_DOM_CONTRACT,
    TIEBA_DETAIL_DOM_CONTRACT,
    TIEBA_FORUM_DOM_CONTRACT,
    TiebaDetailDomContract,
    TiebaDomTargetProvider,
    TiebaTargetCursor,
)
from .targets import TiebaTarget, TiebaTargetKind, parse_tieba_target

__all__ = [
    "TIEBA_CREATOR_DOM_CONTRACT",
    "TIEBA_DETAIL_DOM_CONTRACT",
    "TIEBA_FORUM_DOM_CONTRACT",
    "TIEBA_SEARCH_DOM_CONTRACT",
    "TiebaComment",
    "TiebaCommentCursor",
    "TiebaCommentPage",
    "TiebaCommentsAdapter",
    "TiebaCommentsProvider",
    "TiebaDetailAdapter",
    "TiebaDetailDomContract",
    "TiebaDetailProvider",
    "TiebaDomCommentsProvider",
    "TiebaDomContract",
    "TiebaDomSearchProvider",
    "TiebaDomTargetProvider",
    "TiebaSearchAdapter",
    "TiebaSearchCursor",
    "TiebaSearchProvider",
    "TiebaTarget",
    "TiebaTargetCursor",
    "TiebaTargetKind",
    "TiebaTargetThreadsAdapter",
    "TiebaTargetThreadsProvider",
    "TiebaThread",
    "TiebaThreadPage",
    "normalize_tieba_thread",
    "parse_tieba_target",
]
