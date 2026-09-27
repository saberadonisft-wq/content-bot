import type {
  MediaMetadata,
  OverlayLayout,
  SubtitleParseResultV2,
  SubtitleOcrRegion,
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
  connection_id: string;
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
  caption_original?: string | null;
  caption_edited?: string | null;
  caption_edited_at?: string | null;
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

export type GeminiChunkStatus = {
  chunk_id: string; state: string; message?: string; key_id?: string; key_name?: string; model?: string;
  quality_errors?: string[]; repair_attempt?: number;
};

export type SubtitleExtractionResult = {
  device?: string;
  compute_type?: string;
  detected_language?: string;
  region?: SubtitleOcrRegion;
  document: SubtitleParseResultV2['document'];
  version_id?: string;
  segment_count?: number;
  translated_count?: number;
  total_count?: number;
  warnings?: SubtitleParseResultV2['warnings'];
  srt?: string;
};

export type SubtitleAsrRuntimeStatus = {
  dependency_ready: boolean; allow_download: boolean; message: string | null;
  models: { id: string; state: 'ready' | 'missing' | 'incomplete' }[];
  devices: { id: 'cpu' | 'cuda'; compute_types: string[] }[];
  runtimes?: { cuda?: { ready: boolean; message: string | null } };
};

export type SubtitleJob<Result = SubtitleExtractionResult & Partial<Omit<SubtitleAlignmentResult, 'document' | 'warnings'>>> = {
  id: string;
  kind: "alignment" | "generation" | "render" | "review" | "ocr" | "asr" | "translation" | "scene";
  dedupe_key: string;
  state: "queued" | "running" | "succeeded" | "failed" | "canceled";
  progress: number;
  phase: string;
  message: string;
  details?: {
    version?: number;
    total?: number;
    completed?: number;
    chunks?: GeminiChunkStatus[];
    translated_count?: number;
    total_cues?: number;
    resume_available?: boolean;
    partial_version_id?: string;
    cache_hits?: number;
  };
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  result?: Result | null;
};

export type SubtitleSceneChunk = {
  id: string;
  start_s: number;
  end_s: number;
  start_ms: number;
  end_ms: number;
  duration_s: number;
};

export type SubtitleSceneDetectionResult = {
  video_id: string;
  source_fingerprint: string;
  duration_s: number;
  scene_cuts_s: number[];
  chunks: SubtitleSceneChunk[];
  parameters: {
    video_id: string;
    threshold: number;
    min_scene_len_s: number;
    target_duration_s: number;
    min_duration_s: number;
    max_duration_s: number;
  };
};

export type SubtitleSceneExportFile = {
  chunk_index: number;
  start_ms: number;
  end_ms: number;
  video_url: string;
  srt_url?: string;
  json_url?: string;
};

export type SubtitleSceneExportResult = {
  video_id: string;
  source_fingerprint: string;
  manifest_url: string;
  files: SubtitleSceneExportFile[];
};

export type SubtitleSceneJob = Omit<SubtitleJob, "result"> & {
  result?: SubtitleSceneDetectionResult | SubtitleSceneExportResult | null;
};

export type SubtitleOcrOptions = {
  auto_probe?: boolean;
  source_language?: string;
  sample_fps?: number;
  min_duration_ms?: number;
  max_gap_ms?: number;
};

export type SubtitleAsrOptions = {
  source_language?: string;
  model?: string;
  device?: "auto" | "cpu" | "cuda";
  compute_type?: string;
};

export type SubtitleTranslateOptions = {
  target_language?: string;
  bilingual?: boolean;
  model?: string;
  batch_size?: number;
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
  source_id?: string | null;
  provider_id?: string | null;
  external_id?: string | null;
  media_id?: string | null;
  part_index?: number | null;
  creator_id?: string | null;
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
  connection_id?: string | null;
  intent_key?: string | null;
  provenance?: Record<string, string | number> | null;
  created_at: string;
  updated_at: string;
};

export type AcquisitionRun = {
  id: string;
  can_continue?: boolean;
  pagination?: {
    kind: string;
    generation: number;
    next_offset: number;
    exhausted: boolean;
    last_stop_reason?: string;
  };
  parent_run_id?: string;
  children?: {
    id: string;
    target: string;
    state: string;
    provider_id: string;
    error_code: string | null;
    error: string | null;
    retry_after?: number | null;
    stop_reason: string | null;
    counters: { scanned: number; new: number };
  }[];
  mode: "video" | "creator" | "playlist" | "search";
  provider_id: string;
  source_id: string | null;
  state: string;
  phase: string;
  stop_reason: string | null;
  error_code: string | null;
  error: string | null;
  retry_after?: number | null;
  request: Record<string, unknown>;
  counters: {
    scanned: number;
    new: number;
    duplicate: number;
    filtered: number;
    unavailable: number;
  };
  created_at: string;
  updated_at: string;
};

export type AcquisitionMode = "video" | "creator" | "playlist" | "search";

export type AcquisitionCapabilityStatus =
  | "ready"
  | "setup_required"
  | "unverified"
  | "unsupported";

export type AcquisitionOperationCapability = {
  status: AcquisitionCapabilityStatus;
  enabled: boolean;
  provider_id: string | null;
  detail: string;
  reason_code: string | null;
  implementation?: string;
  implementation_version?: string;
  availability?: string;
  target_kinds?: string[];
  auth?: string;
  schedule_policy?: "background_safe" | "manual_only" | string;
  coverage?: string;
  limits?: Record<string, number>;
  checked_at?: string;
};

export type AcquisitionSourceCapability = {
  source_id: string;
  label: string;
  operations: Record<string, AcquisitionOperationCapability>;
};

export type AcquisitionCapabilities = {
  version: number;
  feature_enabled?: boolean;
  feature_disabled_reason?: string | null;
  generated_at: string;
  session_bridge: {
    status: AcquisitionCapabilityStatus;
    enabled: boolean;
    reason_code: string;
    detail: string;
  };
  items: AcquisitionSourceCapability[];
};

export type AcquisitionCandidate = {
  id: string;
  source_id: string;
  provider_id: string;
  external_id: string;
  media_id: string;
  part_index?: number | null;
  canonical_url: string;
  title: string;
  uploader: string;
  creator_id: string;
  thumbnail_url: string | null;
  published_at: string | null;
  duration_seconds: number | null;
  media_type: "video" | "audio" | "live";
  availability: string;
  download_available?: boolean;
  download_unavailable_reason?: string | null;
  metrics: Record<string, number>;
  position: number;
  download?: {
    state: string;
    job_id: string | null;
    error: string | null;
  };
};

export type AcquisitionCandidatesPage = {
  run_id: string;
  items: AcquisitionCandidate[];
  total: number;
  offset: number;
  limit: number;
};

export type AcquisitionSelection = {
  id: string;
  idempotency_key: string | null;
  quality: VideoDownloadQuality;
  connection_id?: string | null;
  candidate_ids: string[];
  intent_ids?: string[];
  state: string;
  counts: Record<string, number>;
  total: number;
  created_at: string;
  updated_at: string;
};

export type AcquisitionChannel = {
  id: string;
  source_id: string;
  canonical_url: string;
  label: string;
  target_kind: "creator" | "playlist";
  connection_id?: string | null;
  enabled: boolean;
  subscription: {
    enabled: boolean;
    initial_policy: "baseline" | "backfill";
    auto_download: boolean;
    interval_minutes: number;
    quality?: VideoDownloadQuality;
    max_items?: number;
    active_run_id?: string | null;
    last_run_id?: string | null;
    last_scanned_at?: string | null;
    next_run_at?: string | null;
    last_status?: string | null;
    last_error?: string | null;
  };
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
