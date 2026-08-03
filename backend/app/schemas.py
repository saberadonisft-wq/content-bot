from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class KeywordInput(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    include_terms: list[str] = Field(default_factory=list)
    exclude_terms: list[str] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    enabled: bool = True
    interval_minutes: int = Field(default=360, ge=30, le=10080)
    max_items_per_source: int = Field(default=500, ge=1, le=500)


class KeywordOutput(KeywordInput):
    id: int
    next_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RunRequest(BaseModel):
    keyword_id: int
    source_ids: list[str] | None = None
    trigger: Literal["manual", "schedule"] = "manual"


class SourceOutput(BaseModel):
    id: str
    label: str
    group: str
    state: str
    detail: str
    global_search: bool
    watchlist_filter: bool
    requires_login: bool
    interaction_fields: list[str]


class SourceRunOutput(BaseModel):
    id: str
    source_id: str
    state: str
    phase: str = "queued"
    progress_mode: Literal["determinate", "indeterminate"] = "determinate"
    progress_current: int = 0
    progress_total: int | None = None
    progress_percent: float | None = None
    message: str | None = None
    browser_state: str | None = None
    fetched_count: int
    ingested_count: int
    started_at: datetime | None
    finished_at: datetime | None
    heartbeat_at: datetime | None = None
    error_message: str | None


class BatchOutput(BaseModel):
    id: str
    keyword_id: int
    trigger: str
    state: str
    started_at: datetime | None
    finished_at: datetime | None
    error_message: str | None
    source_runs: list[SourceRunOutput] = Field(default_factory=list)


class LanguageInsight(BaseModel):
    code: str
    label: str
    confidence: float
    reason: str


class SentimentInsight(BaseModel):
    label: Literal["positive", "negative", "mixed", "neutral"]
    score: float
    reasons: list[str] = Field(default_factory=list)


class TopicInsight(BaseModel):
    id: str
    label: str
    reasons: list[str] = Field(default_factory=list)


class ContentInsights(BaseModel):
    language: LanguageInsight
    sentiment: SentimentInsight
    topics: list[TopicInsight] = Field(default_factory=list)
    method: str


class InsightBucket(BaseModel):
    id: str
    label: str
    count: int
    percentage: float


class InsightTopItem(BaseModel):
    id: int
    title: str
    source_id: str
    canonical_url: str
    trend_score: float
    language: str
    sentiment: str
    topics: list[str] = Field(default_factory=list)


class InsightSummaryOutput(BaseModel):
    keyword_id: int
    generated_at: datetime
    filters: dict[str, str] = Field(default_factory=dict)
    total_items: int
    topic_coverage_count: int
    topic_coverage_percentage: float
    sources: list[InsightBucket]
    languages: list[InsightBucket]
    sentiments: list[InsightBucket]
    topics: list[InsightBucket]
    top_signals: list[InsightBucket]
    top_items: list[InsightTopItem]
    method: str
    caveat: str


class ClusterTopic(BaseModel):
    label: str
    count: int


class TrendClusterItem(BaseModel):
    id: int
    title: str
    source_id: str
    item_host: str
    canonical_url: str
    trend_score: float
    published_at: datetime | None
    language: str
    sentiment: str
    topics: list[str] = Field(default_factory=list)


class TrendCluster(BaseModel):
    id: str
    label: str
    item_count: int
    source_ids: list[str]
    item_hosts: list[str]
    origin_count: int
    max_trend_score: float
    average_trend_score: float
    latest_at: datetime | None
    sentiments: dict[str, int] = Field(default_factory=dict)
    topics: list[ClusterTopic] = Field(default_factory=list)
    match_reasons: list[str] = Field(default_factory=list)
    items: list[TrendClusterItem] = Field(default_factory=list)


class TrendClustersOutput(BaseModel):
    keyword_id: int
    generated_at: datetime
    filters: dict[str, str] = Field(default_factory=dict)
    total_items: int
    clustered_items: int
    cluster_count: int
    returned_clustered_items: int
    returned_cluster_count: int
    truncated: bool
    clusters: list[TrendCluster] = Field(default_factory=list)
    method: str
    caveat: str


class ItemOutput(BaseModel):
    id: int
    source_id: str
    canonical_url: str
    title: str
    body_snippet: str
    author: str
    hashtags: list[str]
    locale: str | None
    published_at: datetime | None
    metrics: dict[str, int]
    relevance_score: float
    trend_score: float
    match_reasons: list[str]
    insights: ContentInsights
    first_seen_at: datetime
    last_seen_at: datetime


class PagedItems(BaseModel):
    items: list[ItemOutput]
    total: int


class TrendPoint(BaseModel):
    captured_at: datetime
    engagement: int


class TrendOutput(BaseModel):
    item_id: int
    title: str
    source_id: str
    trend_score: float
    points: list[TrendPoint]
