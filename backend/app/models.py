from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class Keyword(Base):
    __tablename__ = "keywords"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(180), unique=True, index=True)
    include_terms_json: Mapped[str] = mapped_column(Text, default="[]")
    exclude_terms_json: Mapped[str] = mapped_column(Text, default="[]")
    source_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=360)
    max_items_per_source: Mapped[int] = mapped_column(Integer, default=500)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    batches: Mapped[list[CrawlBatch]] = relationship(back_populates="keyword", cascade="all, delete-orphan")
    matches: Mapped[list[ItemKeywordMatch]] = relationship(back_populates="keyword", cascade="all, delete-orphan")


class CrawlBatch(Base):
    __tablename__ = "crawl_batches"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    keyword_id: Mapped[int] = mapped_column(ForeignKey("keywords.id"), index=True)
    trigger: Mapped[str] = mapped_column(String(20))
    state: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    keyword: Mapped[Keyword] = relationship(back_populates="batches")
    source_runs: Mapped[list[SourceRun]] = relationship(back_populates="batch", cascade="all, delete-orphan")


class SourceRun(Base):
    __tablename__ = "source_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    batch_id: Mapped[str] = mapped_column(ForeignKey("crawl_batches.id"), index=True)
    source_id: Mapped[str] = mapped_column(String(40), index=True)
    state: Mapped[str] = mapped_column(String(24), default="queued", index=True)
    checkpoint_json: Mapped[str] = mapped_column(Text, default="{}")
    fetched_count: Mapped[int] = mapped_column(Integer, default=0)
    ingested_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    batch: Mapped[CrawlBatch] = relationship(back_populates="source_runs")


class ContentItem(Base):
    __tablename__ = "content_items"
    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_content_source_external"),
        Index("ix_content_source_published", "source_id", "published_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[str] = mapped_column(String(40), index=True)
    external_id: Mapped[str] = mapped_column(String(255))
    canonical_url: Mapped[str] = mapped_column(Text)
    title: Mapped[str] = mapped_column(Text, default="")
    body_snippet: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(255), default="")
    hashtags_json: Mapped[str] = mapped_column(Text, default="[]")
    locale: Mapped[str | None] = mapped_column(String(24), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    metrics_json: Mapped[str] = mapped_column(Text, default="{}")
    raw_payload_json: Mapped[str] = mapped_column(Text, default="{}")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)

    matches: Mapped[list[ItemKeywordMatch]] = relationship(back_populates="content_item", cascade="all, delete-orphan")
    snapshots: Mapped[list[MetricSnapshot]] = relationship(back_populates="content_item", cascade="all, delete-orphan")


class ItemKeywordMatch(Base):
    __tablename__ = "item_keyword_matches"
    __table_args__ = (UniqueConstraint("content_item_id", "keyword_id", name="uq_item_keyword"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_item_id: Mapped[int] = mapped_column(ForeignKey("content_items.id"), index=True)
    keyword_id: Mapped[int] = mapped_column(ForeignKey("keywords.id"), index=True)
    relevance_score: Mapped[float] = mapped_column(Float, default=0)
    trend_score: Mapped[float] = mapped_column(Float, default=0)
    match_reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    content_item: Mapped[ContentItem] = relationship(back_populates="matches")
    keyword: Mapped[Keyword] = relationship(back_populates="matches")


class MetricSnapshot(Base):
    __tablename__ = "metric_snapshots"
    __table_args__ = (UniqueConstraint("content_item_id", "captured_at", name="uq_snapshot_time"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    content_item_id: Mapped[int] = mapped_column(ForeignKey("content_items.id"), index=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    view_count: Mapped[int] = mapped_column(Integer, default=0)
    like_count: Mapped[int] = mapped_column(Integer, default=0)
    comment_count: Mapped[int] = mapped_column(Integer, default=0)
    share_count: Mapped[int] = mapped_column(Integer, default=0)
    favorite_count: Mapped[int] = mapped_column(Integer, default=0)

    content_item: Mapped[ContentItem] = relationship(back_populates="snapshots")
