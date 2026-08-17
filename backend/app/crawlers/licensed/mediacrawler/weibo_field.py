# Copyright (c) 2025 relakkes@gmail.com
#
# This file is derived from MediaCrawler
# (media_platform/weibo/field.py) at commit
# 071c8c0acaece3e82f2532cffb19faeddc9ec1c3.
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1.
# See LICENSE and NOTICE.md in this directory.

"""Small, copied Weibo search-type vocabulary used by the licensed facade."""

from enum import Enum


class SearchType(Enum):
    DEFAULT = "1"
    REAL_TIME = "61"
    POPULAR = "60"
    VIDEO = "64"

