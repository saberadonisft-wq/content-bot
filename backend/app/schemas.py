from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, StrictInt, model_validator

from .services.speech_evidence import AlignmentMethod, SpeechEvidence


class ChannelSubscription(BaseModel):
    id: str | None = None
    url: str = Field(min_length=8, max_length=2048)
    normalized_url: str | None = None
    label: str = Field(default="", max_length=120)
    source_id: str | None = None
    mode: str | None = None
    enabled: bool = True
    include_replies: bool = False
    include_reposts: bool = False
    last_scanned_at: datetime | None = None
    last_status: str | None = None
    last_error: str | None = None


class LiveWallConfig(BaseModel):
    channel_ids: list[str] = Field(default_factory=list, max_length=100)
    slots: Literal[1, 2, 4, 6] = 4

    @model_validator(mode="after")
    def validate_channel_ids(self) -> LiveWallConfig:
        if len(set(self.channel_ids)) != len(self.channel_ids):
            raise ValueError("live_wall channel_ids must be unique")
        if any(not value.strip() or len(value) > 128 for value in self.channel_ids):
            raise ValueError("live_wall channel_ids contain an invalid identifier")
        return self


class KeywordInput(BaseModel):
    name: str = Field(min_length=2, max_length=180)
    include_terms: list[str] = Field(default_factory=list, max_length=50)
    exclude_terms: list[str] = Field(default_factory=list, max_length=50)
    source_ids: list[str] = Field(default_factory=list, max_length=20)
    channels: list[ChannelSubscription] = Field(default_factory=list, max_length=100)
    enabled: bool = True
    interval_minutes: int = Field(default=360, ge=30, le=10080)
    max_items_per_source: int = Field(default=500, ge=1, le=500)


class KeywordOutput(KeywordInput):
    id: int
    live_wall: LiveWallConfig = Field(default_factory=LiveWallConfig)
    next_run_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class RunRequest(BaseModel):
    keyword_id: int
    source_ids: list[str] | None = None
    channel_ids: list[str] | None = None
    trigger: Literal["manual", "schedule"] = "manual"


class SourceOutput(BaseModel):
    schema_version: str = "cbce.source-catalog.v1"
    id: str
    label: str
    group: str
    order: int = 0
    primary_operation: str = "search"
    state: str
    detail: str
    global_search: bool
    watchlist_filter: bool
    requires_login: bool
    interaction_fields: list[str]
    metrics: list[dict[str, Any]] = Field(default_factory=list)
    legacy_aliases: list[str] = Field(default_factory=list)
    provider_selection: list[str] = Field(default_factory=list)
    operations: list[dict[str, Any]] = Field(default_factory=list)
    health_summary: dict[str, Any] = Field(default_factory=dict)
    coverage_disclaimer: str | None = None


class SourceRunOutput(BaseModel):
    id: str
    source_id: str
    channel_id: str | None = None
    channel_url: str | None = None
    channel_label: str | None = None
    state: str
    phase: str = "queued"
    progress_mode: Literal["determinate", "indeterminate"] = "determinate"
    progress_current: int = 0
    progress_total: int | None = None
    progress_percent: float | None = None
    message: str | None = None
    browser_state: str | None = None
    provider_id: str | None = None
    operation: str | None = None
    error_code: str | None = None
    retryable: bool | None = None
    retry_after_seconds: float | None = None
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
    session_number: int = 1
    new_item_count: int = 0
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
    caption_original: str | None = None
    caption_edited: str | None = None
    caption_edited_at: datetime | None = None


class PagedItems(BaseModel):
    items: list[ItemOutput]
    total: int


class CaptionCleanRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20_000)


class CaptionCleanResponse(BaseModel):
    original_text: str
    cleaned_text: str
    hashtags: list[str] = Field(default_factory=list, max_length=100)
    safe_filename: str = Field(min_length=1, max_length=80)
    changed: bool


class CaptionApplyRequest(BaseModel):
    original_text: str | None = Field(default=None, max_length=20_000)
    edited_text: str = Field(min_length=1, max_length=20_000)


class CaptionApplyResponse(BaseModel):
    item_id: int
    caption_original: str
    caption_edited: str
    caption_edited_at: datetime


class TrendPoint(BaseModel):
    captured_at: datetime
    engagement: int


class TrendOutput(BaseModel):
    item_id: int
    title: str
    source_id: str
    trend_score: float
    points: list[TrendPoint]


TimingSource = Literal[
    "manual",
    "gemini_estimate",
    "asr_word",
    "forced_alignment",
    "imported_srt",
    "imported_vtt",
    "ocr",
    "asr",
]


class SubtitleWordV2(BaseModel):
    id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
    )
    text: str = Field(min_length=1, max_length=500)
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(ge=0)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    alignment_method: AlignmentMethod | None = None

    @model_validator(mode="after")
    def validate_time_range(self) -> SubtitleWordV2:
        if self.end_ms <= self.start_ms:
            raise ValueError("Word end_ms must be after start_ms")
        return self


class SubtitlePosition(BaseModel):
    x: float = Field(ge=0, le=100, allow_inf_nan=False)
    y: float = Field(ge=0, le=100, allow_inf_nan=False)


class SubtitleCueV2(BaseModel):
    layout: SubtitlePosition | None = None
    id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$"
    )
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(ge=0)
    speech_start_ms: StrictInt | None = Field(default=None, ge=0)
    speech_end_ms: StrictInt | None = Field(default=None, ge=0)
    speech_evidence: SpeechEvidence | None = None
    text: str = Field(min_length=1, max_length=4000)
    secondary_text: str | None = Field(default=None, max_length=4000)
    source_text: str | None = Field(default=None, max_length=4000)
    source_language: str | None = Field(default=None, max_length=32)
    content_source: Literal["audio", "screen", "mixed", "unknown"] = "unknown"
    origin_chunk_id: str | None = Field(default=None, max_length=64)
    origin_model: str | None = Field(default=None, max_length=128)
    locked: bool = False
    words: list[SubtitleWordV2] | None = Field(default=None, max_length=500)
    timing_source: TimingSource = "gemini_estimate"
    timing_precision_ms: StrictInt = Field(default=1000, ge=1, le=60_000)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    needs_review: bool = False
    revision: StrictInt = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_time_range(self) -> SubtitleCueV2:
        if self.end_ms <= self.start_ms:
            raise ValueError("Subtitle end_ms must be after start_ms")
        if (self.speech_start_ms is None) != (self.speech_end_ms is None):
            raise ValueError("speech_start_ms and speech_end_ms must be set together")
        if (
            self.speech_start_ms is not None
            and self.speech_end_ms is not None
            and self.speech_start_ms >= self.speech_end_ms
        ):
            raise ValueError("Speech timing must be a non-empty interval")
        if self.words:
            seen_ids: set[str] = set()
            for word in self.words:
                if word.id in seen_ids:
                    raise ValueError("Word IDs must be unique inside a cue")
                seen_ids.add(word.id)
                in_display = self.start_ms <= word.start_ms < word.end_ms <= self.end_ms
                in_speech = (self.speech_start_ms is not None and self.speech_end_ms is not None
                             and self.speech_start_ms <= word.start_ms < word.end_ms <= self.speech_end_ms)
                if not in_display and not in_speech:
                    raise ValueError("Word timing must be contained by its cue")
        return self


class SubtitleDocumentV2(BaseModel):
    schema_version: Literal[2] = 2
    media_fingerprint: str | None = Field(default=None, min_length=1, max_length=128)
    document_role: Literal['source', 'translation'] | None = None
    source_revision: StrictInt | None = Field(default=None, ge=0)
    source_run_id: str | None = Field(default=None, max_length=64)
    translation_models: list[str] = Field(default_factory=list, max_length=10)
    revision: StrictInt = Field(default=0, ge=0)
    run_id: str | None = Field(default=None, max_length=64)
    language: str = Field(
        default="vi", min_length=2, max_length=32, pattern=r"^[A-Za-z0-9-]+$"
    )
    timebase: Literal["milliseconds"] = "milliseconds"
    timing_source: TimingSource = "gemini_estimate"
    timing_precision_ms: StrictInt = Field(default=1000, ge=1, le=60_000)
    segments: list[SubtitleCueV2] = Field(default_factory=list, max_length=20_000)

    @model_validator(mode="after")
    def validate_ids(self) -> SubtitleDocumentV2:
        ids = [cue.id for cue in self.segments]
        if len(ids) != len(set(ids)):
            raise ValueError("Subtitle cue IDs must be unique")
        return self


class SubtitleWarning(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    message: str = Field(min_length=1, max_length=500)
    cue_id: str | None = Field(default=None, max_length=64)
    related_cue_id: str | None = Field(default=None, max_length=64)
    delta_ms: StrictInt | None = None


class SubtitleParseRequestV2(BaseModel):
    text: str = Field(min_length=1, max_length=8_000_000)
    media_duration_ms: StrictInt | None = Field(default=None, gt=0)


class SubtitleParseResponseV2(BaseModel):
    document: SubtitleDocumentV2
    warnings: list[SubtitleWarning] = Field(default_factory=list)
    srt: str
    count: int


class SubtitleTimeMap(BaseModel):
    trim_start_ms: StrictInt = Field(default=0, ge=0)
    trim_end_ms: StrictInt | None = Field(default=None, gt=0)
    video_speed: Decimal = Field(default=Decimal(1), gt=0, le=4)

    @model_validator(mode="after")
    def validate_trim_range(self) -> SubtitleTimeMap:
        if self.trim_end_ms is not None and self.trim_end_ms <= self.trim_start_ms:
            raise ValueError("trim_end_ms must be after trim_start_ms")
        return self


class SubtitleTransformRequestV2(BaseModel):
    document: SubtitleDocumentV2
    time_map: SubtitleTimeMap = Field(default_factory=SubtitleTimeMap)


class SubtitleTransformResponseV2(BaseModel):
    document: SubtitleDocumentV2
    warnings: list[SubtitleWarning] = Field(default_factory=list)


class MediaMetadata(BaseModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    file_size_bytes: StrictInt = Field(gt=0)
    duration_ms: StrictInt = Field(gt=0)
    source_start_ms: StrictInt = 0
    time_base_numerator: StrictInt = Field(gt=0)
    time_base_denominator: StrictInt = Field(gt=0)
    frame_rate_numerator: StrictInt = Field(gt=0)
    frame_rate_denominator: StrictInt = Field(gt=0)
    average_fps: float = Field(gt=0, le=1000)
    frame_count: StrictInt = Field(gt=0)
    is_vfr: bool
    frame_pts_ms: list[StrictInt] = Field(default_factory=list)
    frame_index_source: Literal["packet_pts", "average_fps"] = "packet_pts"
    width: StrictInt = Field(gt=0)
    height: StrictInt = Field(gt=0)
    rotation: StrictInt = Field(default=0, ge=-180, le=180)
    video_codec: str = Field(min_length=1, max_length=64)
    has_audio: bool
    audio_codec: str | None = Field(default=None, max_length=64)
    audio_sample_rate: StrictInt | None = Field(default=None, gt=0)
    audio_channels: StrictInt | None = Field(default=None, gt=0, le=64)
    audio_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_frame_index(self) -> MediaMetadata:
        if self.is_vfr and not self.frame_pts_ms:
            raise ValueError("VFR media must include frame_pts_ms")
        if self.frame_pts_ms != sorted(set(self.frame_pts_ms)):
            raise ValueError("frame_pts_ms must be strictly increasing")
        if self.frame_pts_ms and self.frame_pts_ms[0] != 0:
            raise ValueError("frame_pts_ms must be normalized to zero")
        if self.has_audio and not self.audio_codec:
            raise ValueError("Audio metadata must include audio_codec")
        return self


class SubtitleUploadResponse(BaseModel):
    video_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    filename: str = Field(min_length=1, max_length=255)
    video_url: str
    media: MediaMetadata


class SubtitleOverlayUploadResponse(BaseModel):
    overlay_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    filename: str = Field(min_length=1, max_length=255)
    overlay_url: str
    size_bytes: StrictInt = Field(gt=0)


class SubtitleOverlayRenderOptions(BaseModel):
    overlay_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    x: float = Field(default=14, ge=0, le=100)
    y: float = Field(default=12, ge=0, le=100)
    width: float = Field(default=22, ge=4, le=90)


class SubtitleMaskRegion(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")
    shape: Literal["rectangle", "rounded", "ellipse", "band"] = "rectangle"
    effect: Literal["blur", "pixelate", "solid", "darken"] = "blur"
    x: float = Field(default=20, ge=0, le=100)
    y: float = Field(default=72, ge=0, le=100)
    width: float = Field(default=60, ge=4, le=100)
    height: float = Field(default=12, ge=3, le=100)
    strength: float = Field(default=22, ge=1, le=40)
    opacity: float = Field(default=1, ge=0.05, le=1)
    feather: float = Field(default=2, ge=0, le=20)
    corner_radius: float = Field(
        default=14,
        ge=0,
        le=50,
        validation_alias="cornerRadius",
    )
    color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")

    @model_validator(mode="after")
    def validate_mask_bounds(self) -> SubtitleMaskRegion:
        if self.shape == "band":
            self.x = 0
            self.width = 100
        if self.x + self.width > 100.000_001:
            raise ValueError("Mask width exceeds the video frame")
        if self.y + self.height > 100.000_001:
            raise ValueError("Mask height exceeds the video frame")
        return self


class SubtitleAlignmentOptions(BaseModel):
    engine: Literal["auto", "energy", "faster_whisper"] = "auto"
    lead_in_ms: StrictInt = Field(default=60, ge=0, le=1000)
    tail_ms: StrictInt = Field(default=100, ge=0, le=1000)
    window_padding_ms: StrictInt = Field(default=650, ge=0, le=5000)
    max_window_ms: StrictInt = Field(default=30_000, ge=5000, le=120_000)
    force_manual: bool = False
    preserve_display: bool = False
    source_language: str | None = Field(default=None, pattern=r"^[a-z]{2,3}(?:-[A-Za-z]{2,4})?$")
    max_shift_ms: StrictInt = Field(default=1000, ge=0, le=10_000)


class SubtitleAlignmentRequest(BaseModel):
    video_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    document: SubtitleDocumentV2
    cue_ids: list[str] | None = Field(default=None, max_length=20_000)
    options: SubtitleAlignmentOptions = Field(default_factory=SubtitleAlignmentOptions)

    @model_validator(mode="after")
    def validate_cue_ids(self) -> SubtitleAlignmentRequest:
        if self.cue_ids is not None:
            if len(self.cue_ids) != len(set(self.cue_ids)):
                raise ValueError("cue_ids must be unique")
            document_ids = {cue.id for cue in self.document.segments}
            if any(cue_id not in document_ids for cue_id in self.cue_ids):
                raise ValueError("cue_ids contains an unknown cue")
        return self


class GeminiChunkPolicy(BaseModel):
    target_ms: StrictInt = Field(default=120_000, ge=30_000, le=1_800_000)
    max_chunk_ms: StrictInt = Field(default=600_000, ge=30_000, le=1_800_000)
    min_pause_ms: StrictInt = Field(default=800, ge=100, le=10_000)
    context_ms: StrictInt = Field(default=2000, ge=0, le=10_000)

    @model_validator(mode="after")
    def ordered_limits(self):
        if self.target_ms > self.max_chunk_ms:
            raise ValueError("Target chunk duration exceeds resource limit")
        return self


class VoiceSyncApplyRequest(BaseModel):
    video_id: str = Field(pattern=r'^[a-f0-9]{12,32}$')
    voice_revision: StrictInt = Field(ge=0)
    document: SubtitleDocumentV2
    clip_ids: list[str] = Field(min_length=1, max_length=40)


class VoiceSyncAuditRequest(VoiceSyncApplyRequest):
    align_source: bool = True


class GeminiSubtitleOptions(BaseModel):
    bilingual: bool = True
    # Kept for old drafts/clients; scheduling now uses every enabled key.
    max_concurrent: StrictInt = Field(default=0, ge=0)
    chunk_policy: GeminiChunkPolicy | None = None
    shared_context: str = Field(default="", max_length=8000)
    alignment_mode: Literal["off", "review", "all"] = "off"
    alignment_engine: Literal["energy", "faster_whisper"] = "faster_whisper"
    model: str | None = Field(
        default=None,
        max_length=128,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$",
        description="Gemini base model id without the models/ prefix",
    )


class GeminiSubtitleRequest(BaseModel):
    video_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    options: GeminiSubtitleOptions = Field(default_factory=GeminiSubtitleOptions)
    regenerate: bool = False
    current_document: SubtitleDocumentV2 | None = None


class SubtitleOcrRegion(BaseModel):
    x: float = Field(default=10.0, ge=0.0, le=100.0)
    y: float = Field(default=75.0, ge=0.0, le=100.0)
    width: float = Field(default=80.0, ge=1.0, le=100.0)
    height: float = Field(default=15.0, ge=1.0, le=100.0)

    @model_validator(mode="after")
    def validate_bounds(self) -> SubtitleOcrRegion:
        if self.x + self.width > 100.000_001:
            raise ValueError("Region width exceeds bounds")
        if self.y + self.height > 100.000_001:
            raise ValueError("Region height exceeds bounds")
        return self


class SubtitleOcrRequest(BaseModel):
    video_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    region: SubtitleOcrRegion = Field(default_factory=SubtitleOcrRegion)
    auto_probe: bool = Field(default=False)
    source_language: str | None = Field(default=None, max_length=32)
    sample_fps: float = Field(default=5.0, ge=1.0, le=30.0)
    min_duration_ms: StrictInt = Field(default=300, ge=50, le=10_000)
    max_gap_ms: StrictInt = Field(default=250, ge=50, le=5_000)


class SubtitleAsrRequest(BaseModel):
    video_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    source_language: str | None = Field(default=None, max_length=32)
    model: str = Field(default="small", max_length=64)
    device: Literal["auto", "cpu", "cuda"] = "auto"
    compute_type: str = "auto"


class SubtitleTranslateRequest(BaseModel):
    video_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    document: SubtitleDocumentV2
    bilingual: bool = True
    target_language: str = Field(default="vi", max_length=32)
    model: str | None = Field(default=None, max_length=128)
    batch_size: StrictInt = Field(default=20, ge=1, le=100)


class SubtitleExportSourceRequest(BaseModel):
    document: SubtitleDocumentV2


class SubtitleJobResponse(BaseModel):
    id: str = Field(pattern=r"^[a-f0-9]{20}$")
    kind: Literal["alignment", "generation", "render", "review", "ocr", "asr", "translation", "separation", "scene"]
    dedupe_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: Literal["queued", "running", "succeeded", "failed", "canceled"]
    progress: StrictInt = Field(ge=0, le=100)
    phase: str = Field(max_length=64)
    message: str = Field(max_length=500)
    cancel_requested: bool = False
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    error: str | None = Field(default=None, max_length=2000)
    result: dict[str, Any] | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class SubtitleVideoSegment(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def validate_segment_range(self) -> SubtitleVideoSegment:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be after start_ms")
        return self


class SubtitleSceneDetectRequest(BaseModel):
    video_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    threshold: float = Field(default=27, ge=1, le=100, allow_inf_nan=False)
    min_scene_len_s: float = Field(default=1.5, ge=0.5, le=30, allow_inf_nan=False)
    target_duration_s: float = Field(default=45, ge=5, le=180, allow_inf_nan=False)
    min_duration_s: float = Field(default=25, ge=1, le=180, allow_inf_nan=False)
    max_duration_s: float = Field(default=75, ge=5, le=300, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_chunk_durations(self) -> SubtitleSceneDetectRequest:
        if self.min_duration_s > self.target_duration_s:
            raise ValueError("min_duration_s must not exceed target_duration_s")
        if self.target_duration_s > self.max_duration_s:
            raise ValueError("target_duration_s must not exceed max_duration_s")
        return self


class SubtitleSceneExportChunk(BaseModel):
    id: str = Field(min_length=1, max_length=80)
    start_ms: StrictInt = Field(ge=0)
    end_ms: StrictInt = Field(gt=0)

    @model_validator(mode="after")
    def validate_range(self) -> SubtitleSceneExportChunk:
        if self.end_ms <= self.start_ms:
            raise ValueError("end_ms must be after start_ms")
        return self


class SubtitleSceneExportRequest(BaseModel):
    video_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    source_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    chunks: list[SubtitleSceneExportChunk] = Field(min_length=1, max_length=100)
    document: SubtitleDocumentV2 | None = None

    @model_validator(mode="after")
    def validate_chunks(self) -> SubtitleSceneExportRequest:
        previous_end = -1
        for chunk in self.chunks:
            if chunk.start_ms < previous_end:
                raise ValueError("chunks must be ordered and non-overlapping")
            previous_end = chunk.end_ms
        return self


class SubtitleRenderOptionsV2(BaseModel):
    render_mode: Literal["precision", "effects"] = "precision"
    profile: Literal["fast", "balanced", "quality"] = "fast"
    encoder: Literal["auto", "nvenc", "qsv", "software"] = "auto"
    font_name: str = Field(
        default="Arimo",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$",
    )
    font_size: int = Field(default=38, ge=12, le=96)
    font_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{6}$")
    bold: bool = True
    italic: bool = False
    underline: bool = False
    strikethrough: bool = False
    uppercase: bool = False
    alignment_type: Literal["left", "center", "right"] = "center"
    outline_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")
    outline_width: int = Field(default=2, ge=0, le=10)
    shadow_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")
    shadow_width: int = Field(default=1, ge=0, le=10)
    bg_enabled: bool = False
    bg_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{6}$")
    bg_opacity: float = Field(default=0.75, ge=0, le=1)
    spacing: float = Field(default=0, ge=-5, le=20, allow_inf_nan=False)
    line_spacing: float = Field(default=1.2, ge=0.8, le=3)
    pos_x: float = Field(default=50, ge=0, le=100)
    pos_y: float = Field(default=78, ge=0, le=100)
    position: Literal["bottom", "middle", "top", "custom"] = "custom"
    aspect_ratio: Literal["16:9", "9:16", "1:1", "original"] = "original"
    bg_fill_type: Literal["blur", "black", "color"] = "blur"
    trim_start_ms: StrictInt = Field(default=0, ge=0)
    trim_end_ms: StrictInt | None = Field(default=None, gt=0)
    video_speed: Decimal = Field(default=Decimal(1), ge=Decimal("0.5"), le=2)
    volume: float = Field(default=1, ge=0, le=2)
    fade_in_ms: StrictInt = Field(default=0, ge=0, le=5000)
    fade_out_ms: StrictInt = Field(default=0, ge=0, le=5000)
    animation: Literal["none", "fade", "rise", "pan", "typewriter"] = "none"
    video_segments: list[SubtitleVideoSegment] = Field(
        default_factory=list,
        max_length=100,
    )

    @model_validator(mode="after")
    def validate_render_timeline(self) -> SubtitleRenderOptionsV2:
        if self.trim_end_ms is not None and self.trim_end_ms <= self.trim_start_ms:
            raise ValueError("trim_end_ms must be after trim_start_ms")
        if self.render_mode == "precision" and self.animation != "none":
            raise ValueError("Precision mode does not quantize ASS animation timing")
        for previous, current in zip(self.video_segments, self.video_segments[1:]):
            if current.start_ms < previous.end_ms:
                raise ValueError("video_segments must be ordered and non-overlapping")
        return self


class SubtitleRenderRequestV2(BaseModel):
    voice_project_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{12,32}$")
    voice_revision: int | None = Field(default=None, ge=0)
    video_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    document: SubtitleDocumentV2
    options: SubtitleRenderOptionsV2 = Field(default_factory=SubtitleRenderOptionsV2)
    overlay: SubtitleOverlayRenderOptions | None = None
    masks: list[SubtitleMaskRegion] = Field(default_factory=list, max_length=8)

    @model_validator(mode="after")
    def require_subtitles(self) -> SubtitleRenderRequestV2:
        if (
            not self.document.segments
            and self.overlay is None
            and not self.masks
            and not self.options.video_segments
            and not self.voice_project_id
        ):
            raise ValueError(
                "At least one subtitle cue, overlay, mask, or video segment is required"
            )
        return self


class SubtitleAssPreviewRequestV2(BaseModel):
    video_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    document: SubtitleDocumentV2
    options: SubtitleRenderOptionsV2 = Field(default_factory=SubtitleRenderOptionsV2)


class SubtitleAssPreviewResponse(BaseModel):
    ass: str
    play_res_x: int = Field(gt=0)
    play_res_y: int = Field(gt=0)
    timing_precision_ms: Literal[10] = 10


class SubtitleItem(BaseModel):
    start_time: str = Field(min_length=1, max_length=32)
    end_time: str = Field(min_length=1, max_length=32)
    start_seconds: float = Field(ge=0)
    end_seconds: float = Field(ge=0)
    text: str = Field(min_length=1, max_length=4000)
    secondary_text: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def validate_time_range(self) -> SubtitleItem:
        if self.end_seconds <= self.start_seconds:
            raise ValueError("Subtitle end time must be after start time")
        return self


class SubtitleParseRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500_000)


class SubtitleParseResponse(BaseModel):
    subtitles: list[SubtitleItem] = Field(default_factory=list, max_length=500)
    srt: str
    count: int


class SubtitleBurnOptions(BaseModel):
    font_name: str = Field(
        default="Arial",
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._-]*$",
    )
    font_size: int = Field(default=24, ge=12, le=72)
    font_color: str = Field(default="#FFFFFF", pattern=r"^#[0-9A-Fa-f]{3,6}$")
    bold: bool = Field(default=False)
    italic: bool = Field(default=False)
    underline: bool = Field(default=False)
    strikethrough: bool = Field(default=False)
    uppercase: bool = Field(default=False)
    alignment_type: Literal["left", "center", "right"] = Field(default="center")
    outline_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{3,6}$")
    outline_width: int = Field(default=2, ge=0, le=10)
    shadow_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{3,6}$")
    shadow_width: int = Field(default=0, ge=0, le=10)
    bg_enabled: bool = Field(default=False)
    bg_color: str = Field(default="#000000", pattern=r"^#[0-9A-Fa-f]{3,6}$")
    bg_opacity: float = Field(default=0.75, ge=0.0, le=1.0)
    spacing: float = Field(default=0, ge=-5, le=20, allow_inf_nan=False)
    line_spacing: float = Field(default=1.2, ge=0.8, le=3.0)
    pos_x: float = Field(default=50.0, ge=0.0, le=100.0)
    pos_y: float = Field(default=85.0, ge=0.0, le=100.0)
    position: Literal["bottom", "middle", "top", "custom"] = Field(default="bottom")
    video_speed: float = Field(default=1.0, ge=0.5, le=2.0)
    volume: float = Field(default=1.0, ge=0.0, le=2.0)
    fade_in: float = Field(default=0.0, ge=0.0, le=5.0)
    fade_out: float = Field(default=0.0, ge=0.0, le=5.0)
    aspect_ratio: Literal["16:9", "9:16", "1:1", "original"] = Field(default="16:9")
    bg_fill_type: Literal["blur", "black", "color"] = Field(default="blur")
    trim_start: float = Field(default=0.0, ge=0.0)
    trim_end: float | None = Field(default=None)
    animation: Literal["none", "fade", "rise", "pan", "typewriter"] = Field(
        default="none"
    )

    @model_validator(mode="after")
    def validate_trim_range(self) -> SubtitleBurnOptions:
        if self.trim_end is not None and self.trim_end <= self.trim_start:
            raise ValueError("trim_end must be after trim_start")
        return self


class SubtitleBurnRequest(BaseModel):
    video_id: str = Field(pattern=r"^[a-f0-9]{12,32}$")
    subtitles: list[SubtitleItem] = Field(min_length=1, max_length=500)
    options: SubtitleBurnOptions = Field(default_factory=SubtitleBurnOptions)


class SubtitleBurnResponse(BaseModel):
    video_id: str
    output_filename: str
    video_url: str
    subtitled_video_url: str


class VideoLibraryItem(BaseModel):
    id: str
    filename: str
    type: Literal["original", "subtitled", "scraped"]
    size_bytes: int
    created_at: datetime
    thumbnail_url: str
    video_url: str
    metrics: dict[str, int] = {}
    title: str | None = None
    source_url: str | None = None
    source_id: str | None = None
    provider_id: str | None = None
    external_id: str | None = None
    media_id: str | None = None
    part_index: int | None = None
    creator_id: str | None = None
    platform: str | None = None
    duration: float | None = None
    downloaded: bool = False
