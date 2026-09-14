import type {
  MediaMetadata,
  OverlayLayout,
  SubtitleParseResultV2,
  SubtitleWarning,
  VideoClip
} from "../subtitles/types";
export type {
  MediaMetadata, SubtitleCueV2,
  SubtitleDocumentV2, SubtitleMaskRegion, SubtitleParseResultV2,
  SubtitleTimingSource, SubtitleWarning,
  SubtitleWordV2,
  VideoClip
} from "../subtitles/types";

export type SourceOperation = {
  id: string;
  provider_id: string;
  coverage: "full" | "partial" | "unsupported";
  implementation: "implemented" | "planned";
  availability: string | null;
  enabled: boolean;
  reason_code: string | null;
  detail: string;
  auth_modes: string[];
  schedule_policy: string;
  target_kinds: string[];
  budget_limits: {
    max_items: number;
    max_requests: number;
    deadline_seconds: number;
  } | null;
};

export type SourceMetric = {
  id: string;
  label: string;
  semantics: string;
  legacy_key: string | null;
};

export type Source = {
  schema_version: string;
  id: string;
  label: string;
  group: string;
  order: number;
  primary_operation: string;
  state: string;
  detail: string;
  global_search: boolean;
  watchlist_filter: boolean;
  requires_login: boolean;
  interaction_fields: string[];
  metrics: SourceMetric[];
  legacy_aliases: string[];
  provider_selection: string[];
  operations: SourceOperation[];
  health_summary: {
    state: string;
    detail: string;
    checked_at: string;
    probe: string;
  };
  coverage_disclaimer: string | null;
};

export type TikTokOAuthStatus = {
  state:
  | "setup_required"
  | "disconnected"
  | "connected"
  | "refresh_required"
  | "permission_required"
  | "authorization_expired"
  | "invalid_vault";
  configured: boolean;
  connected: boolean;
  detail: string;
  authorized_username: string;
  scopes: string[];
  access_expires_at: string | null;
  refresh_expires_at: string | null;
};

export type CrawlerLoginStatus = {
  source_id: string;
  state:
  | "idle"
  | "opening"
  | "waiting_for_user"
  | "verifying"
  | "completed"
  | "cancelled"
  | "failed";
  started_at: string | null;
  finished_at: string | null;
  reason_code: string | null;
  detail: string;
  observation_ready: boolean;
  observation_digest: string | null;
};

export type ChannelSubscription = {
  id: string | null;
  url: string;
  normalized_url: string | null;
  label: string;
  source_id: string | null;
  mode: string | null;
  enabled: boolean;
  include_replies: boolean;
  include_reposts: boolean;
  last_scanned_at: string | null;
  last_status: string | null;
  last_error: string | null;
};

export type LiveWallSlots = 1 | 2 | 4 | 6;

export type LiveWallConfig = {
  channel_ids: string[];
  slots: LiveWallSlots;
};

export type Keyword = {
  id: number;
  name: string;
  include_terms: string[];
  exclude_terms: string[];
  source_ids: string[];
  channels: ChannelSubscription[];
  live_wall: LiveWallConfig;
  enabled: boolean;
  interval_minutes: number;
  max_items_per_source: number;
  next_run_at: string | null;
  created_at: string;
  updated_at: string;
};

export type Item = {
  id: number;
  source_id: string;
  canonical_url: string;
  title: string;
  body_snippet: string;
  author: string;
  hashtags: string[];
  locale: string | null;
  published_at: string | null;
  metrics: Record<string, number>;
  relevance_score: number;
  trend_score: number;
  match_reasons: string[];
  insights: {
    language: {
      code: string;
      label: string;
      confidence: number;
      reason: string;
    };
    sentiment: {
      label: "positive" | "negative" | "mixed" | "neutral";
      score: number;
      reasons: string[];
    };
    topics: { id: string; label: string; reasons: string[] }[];
    method: string;
  };
};

export type ItemFilters = {
  source?: string;
  session?: string;
  language?: string;
  sentiment?: string;
  topic?: string;
};

export type InsightBucket = {
  id: string;
  label: string;
  count: number;
  percentage: number;
};

export type InsightTopItem = {
  id: number;
  title: string;
  source_id: string;
  canonical_url: string;
  trend_score: number;
  language: string;
  sentiment: string;
  topics: string[];
};

export type InsightSummary = {
  keyword_id: number;
  generated_at: string;
  filters: Record<string, string>;
  total_items: number;
  topic_coverage_count: number;
  topic_coverage_percentage: number;
  sources: InsightBucket[];
  languages: InsightBucket[];
  sentiments: InsightBucket[];
  topics: InsightBucket[];
  top_signals: InsightBucket[];
  top_items: InsightTopItem[];
  method: string;
  caveat: string;
};

export type TrendClusterItem = {
  id: number;
  title: string;
  source_id: string;
  item_host: string;
  canonical_url: string;
  trend_score: number;
  published_at: string | null;
  language: string;
  sentiment: string;
  topics: string[];
};

export type TrendCluster = {
  id: string;
  label: string;
  item_count: number;
  source_ids: string[];
  item_hosts: string[];
  origin_count: number;
  max_trend_score: number;
  average_trend_score: number;
  latest_at: string | null;
  sentiments: Record<string, number>;
  topics: { label: string; count: number }[];
  match_reasons: string[];
  items: TrendClusterItem[];
};

export type TrendClusters = {
  keyword_id: number;
  generated_at: string;
  filters: Record<string, string>;
  total_items: number;
  clustered_items: number;
  cluster_count: number;
  returned_clustered_items: number;
  returned_cluster_count: number;
  truncated: boolean;
  clusters: TrendCluster[];
  method: string;
  caveat: string;
};

export type Batch = {
  id: string;
  keyword_id: number;
  trigger: string;
  session_number: number;
  new_item_count: number;
  state: string;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  source_runs: SourceRun[];
};

export type SourceRun = {
  id: string;
  source_id: string;
  channel_id: string | null;
  channel_url: string | null;
  channel_label: string | null;
  state: string;
  phase: string;
  progress_mode: "determinate" | "indeterminate";
  progress_current: number;
  progress_total: number | null;
  progress_percent: number | null;
  message: string | null;
  browser_state: string | null;
  provider_id: string | null;
  operation: string | null;
  error_code: string | null;
  retryable: boolean | null;
  retry_after_seconds: number | null;
  fetched_count: number;
  ingested_count: number;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
  error_message: string | null;
};

export type RunProgressEvent = {
  type:
  | "connected"
  | "batch"
  | "source-progress"
  | "source-run"
  | "parser-drift-alert"
  | "item";
  batch_id?: string;
  source_run_id?: string;
  state?: string;
  phase?: string;
  progress_mode?: "determinate" | "indeterminate";
  progress_current?: number | null;
  progress_total?: number | null;
  progress_percent?: number | null;
  fetched_count?: number;
  ingested_count?: number;
  message?: string | null;
  browser_state?: string | null;
  error_message?: string | null;
  error_code?: string | null;
  provider_id?: string | null;
  operation?: string | null;
};

export type SubtitleItem = {
  start_time: string;
  end_time: string;
  start_seconds: number;
  end_seconds: number;
  text: string;
  secondary_text?: string;
};

export type SubtitleBurnOptions = {
  font_name: string;
  font_size: number;
  font_color: string;
  bold: boolean;
  italic: boolean;
  underline?: boolean;
  strikethrough?: boolean;
  uppercase: boolean;
  alignment_type?: "left" | "center" | "right";
  outline_color: string;
  outline_width: number;
  shadow_color: string;
  shadow_width: number;
  bg_enabled: boolean;
  bg_color: string;
  bg_opacity: number;
  spacing: number;
  line_spacing?: number;
  pos_x: number;
  pos_y: number;
  position: "bottom" | "middle" | "top" | "custom";
  video_speed?: number;
  volume?: number;
  fade_in?: number;
  fade_out?: number;
  aspect_ratio?: "16:9" | "9:16" | "1:1" | "original";
  bg_fill_type?: "blur" | "black" | "color";
  trim_start?: number;
  trim_end?: number | null;
  animation?: "none" | "fade" | "rise" | "pan" | "typewriter";
};

export type SubtitleUploadResult = {
  video_id: string;
  filename: string;
  video_url: string;
  media: MediaMetadata;
};

export type SubtitleOverlayUploadResult = {
  overlay_id: string;
  filename: string;
  overlay_url: string;
  size_bytes: number;
};

export type SubtitleOverlayRenderOptions = OverlayLayout & {
  overlay_id: string;
};

export type SubtitleParseResult = {
  subtitles: SubtitleItem[];
  srt: string;
  count: number;
};

export type SubtitleBurnResult = {
  video_id: string;
  output_filename: string;
  video_url: string;
  subtitled_video_url: string;
};

export type SubtitleAlignmentOptions = {
  engine?: "auto" | "energy" | "faster_whisper";
  lead_in_ms?: number;
  tail_ms?: number;
  window_padding_ms?: number;
  max_window_ms?: number;
  force_manual?: boolean;
  preserve_display?: boolean;
  source_language?: string | null;
  max_shift_ms?: number;
};

export type SubtitleAlignmentResult = {
  version_id?: string;
  document: SubtitleParseResultV2["document"];
  warnings: SubtitleParseResultV2["warnings"];
  engine: "energy" | "faster_whisper";
  cache_hit: boolean;
  aligned_cue_count: number;
};

export type GeminiSubtitleOptions = {
  max_concurrent?: number;
  bilingual?: boolean;
  model?: string;
  shared_context?: string;
  chunk_policy?: { target_ms: number; max_chunk_ms: number; min_pause_ms: number; context_ms: number };
  alignment_mode?: "off" | "review" | "all";
  alignment_engine?: "energy" | "faster_whisper";
};

export type GeminiModel = {
  id: string;
  display_name: string;
  description: string;
  input_token_limit: number | null;
  output_token_limit: number | null;
};

export type GeminiModelsResponse = {
  models: GeminiModel[];
  selected_model: string;
};

export type GeminiApiStatus = {
  installed: boolean;
  authenticated: boolean;
  auth_method?: string | null;
  issue?: string | null;
  provider?: "api_direct" | null;
  model?: string | null;
};

export type GeminiSubtitleResult = {
  document: SubtitleParseResultV2["document"];
  warnings: SubtitleParseResultV2["warnings"];
  srt: string;
  segment_count: number;
  processing_seconds?: number | null;
  provider: "gemini_api";
  model: string;
  chunk_count: number;
  actual_models?: string[];
  chunks?: GeminiChunkStatus[];
  version_id?: string;
};

export type GeminiChunkStatus = { chunk_id: string; state: string; message?: string; key_id?: string; key_name?: string; model?: string };

export type SubtitleJob = {
  id: string;
  kind: "alignment" | "generation" | "render" | "review";
  dedupe_key: string;
  state: "queued" | "running" | "succeeded" | "failed" | "canceled";
  progress: number;
  phase: string;
  message: string;
  details?: { version?: number; total?: number; completed?: number; chunks?: GeminiChunkStatus[] };
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  result?: SubtitleAlignmentResult | null;
};

export type GeminiSubtitleJob = Omit<SubtitleJob, "result"> & {
  result?: GeminiSubtitleResult | null;
};

export type SubtitleRenderOptionsV2 = Omit<
  SubtitleBurnOptions,
  "trim_start" | "trim_end" | "fade_in" | "fade_out"
> & {
  render_mode: "precision" | "effects";
  profile: "fast" | "balanced" | "quality";
  encoder: "auto" | "nvenc" | "qsv" | "software";
  trim_start_ms: number;
  trim_end_ms?: number | null;
  fade_in_ms: number;
  fade_out_ms: number;
  video_segments: VideoClip[];
};

export type SubtitleRenderResult = {
  video_id: string;
  output_filename: string;
  subtitled_video_url: string;
  encoder: string;
  cache_hit: boolean;
  audio_copied?: boolean | null;
  output_size_bytes: number;
  duration_ms: number;
  elapsed_seconds?: number;
  realtime_factor?: number;
  fallback_reasons: string[];
  warnings?: SubtitleWarning[];
  overlay_applied?: boolean;
};

export type SubtitleAssPreviewResult = {
  ass: string;
  play_res_x: number;
  play_res_y: number;
  timing_precision_ms: 10;
};

export type SubtitleRenderJob = Omit<SubtitleJob, "result"> & {
  result?: SubtitleRenderResult | null;
};

export type VideoLibraryItem = {
  id: string;
  filename: string;
  type: "original" | "subtitled" | "scraped";
  size_bytes: number;
  created_at: string;
  thumbnail_url: string;
  video_url: string;
  metrics?: Record<string, number>;
  title?: string | null;
  source_url?: string | null;
  platform?: string | null;
  duration?: number | null;
  downloaded?: boolean;
};

export type VideoDownloadQuality = "best" | "1080" | "720" | "480";
export type VideoDownloadJob = {
  id: string;
  url: string;
  quality: VideoDownloadQuality;
  platform: string;
  state: "queued" | "running" | "succeeded" | "failed" | "canceled" | "paused";
  phase: string;
  progress: number | null;
  title: string;
  filename: string;
  downloaded_bytes: number;
  total_bytes: number | null;
  speed: number | null;
  eta: number | null;
  duration: number | null;
  error: string | null;
  created_at: string;
  updated_at: string;
};

export type CredentialStatus = {
  is_master_password_set: boolean;
  is_unlocked: boolean;
  configured_keys: Record<string, boolean>;
  vault_configured_keys: Record<string, boolean>;
  env_configured_keys: Record<string, boolean>;
  credential_sources: Record<string, "vault" | "environment" | "none">;
  masked_keys: Record<string, string>;
  platforms: {
    youtube: boolean;
    x_twitter: boolean;
    reddit: boolean;
    meta_instagram: boolean;
    facebook_page: boolean;
    tiktok: boolean;
    gemini: boolean;
  };
};

export type UpdateCheckResponse = {
  update_available: boolean;
  current_version: string;
  latest_version: string;
  channel?: "stable" | "beta";
  download_url?: string;
  sha256?: string;
  file_size?: number;
  changelog?: string;
  mandatory?: boolean;
  published_at?: string;
};
