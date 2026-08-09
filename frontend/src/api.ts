import { cueToLegacySubtitle } from "./subtitles/model";
import type {
  MediaMetadata,
  OverlayLayout,
  SubtitleCueV2,
  SubtitleParseResultV2,
  SubtitleWarning,
} from "./subtitles/types";

export type {
  SubtitleCueV2,
  SubtitleDocumentV2,
  MediaMetadata,
  SubtitleParseResultV2,
  SubtitleTimingSource,
  SubtitleWarning,
  SubtitleWordV2,
} from "./subtitles/types";

export const API_BASE =
  import.meta.env.VITE_API_BASE_URL ?? "http://127.0.0.1:8000/api/v1";

export const runEventsUrl = (batchId: string) =>
  `${API_BASE}/runs/${batchId}/events`;

export type Source = {
  id: string;
  label: string;
  group: string;
  state: string;
  detail: string;
  global_search: boolean;
  watchlist_filter: boolean;
  requires_login: boolean;
  interaction_fields: string[];
};
export type Keyword = {
  id: number;
  name: string;
  include_terms: string[];
  exclude_terms: string[];
  source_ids: string[];
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
  state: string;
  started_at: string | null;
  finished_at: string | null;
  error_message: string | null;
  source_runs: SourceRun[];
};
export type SourceRun = {
  id: string;
  source_id: string;
  state: string;
  phase: string;
  progress_mode: "determinate" | "indeterminate";
  progress_current: number;
  progress_total: number | null;
  progress_percent: number | null;
  message: string | null;
  browser_state: string | null;
  fetched_count: number;
  ingested_count: number;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
  error_message: string | null;
};

export type RunProgressEvent = {
  type: "connected" | "batch" | "source-progress" | "source-run" | "item";
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
};

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Request failed (${response.status})`);
  }
  return response.status === 204 ? (undefined as T) : response.json();
}

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
};

export type SubtitleAlignmentResult = {
  document: SubtitleParseResultV2["document"];
  warnings: SubtitleParseResultV2["warnings"];
  engine: "energy" | "faster_whisper";
  cache_hit: boolean;
  aligned_cue_count: number;
};

export type SubtitleJob = {
  id: string;
  kind: "alignment" | "render";
  dedupe_key: string;
  state: "queued" | "running" | "succeeded" | "failed" | "canceled";
  progress: number;
  phase: string;
  message: string;
  cancel_requested: boolean;
  created_at: string;
  updated_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  error?: string | null;
  result?: SubtitleAlignmentResult | null;
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
};

export const api = {
  videos: () => request<VideoLibraryItem[]>("/videos"),
  sources: () => request<Source[]>("/sources"),
  keywords: () => request<Keyword[]>("/keywords"),
  createKeyword: (
    payload: Omit<Keyword, "id" | "created_at" | "updated_at" | "next_run_at">,
  ) =>
    request<Keyword>("/keywords", {
      method: "POST",
      body: JSON.stringify(payload),
    }),
  updateKeyword: (
    id: number,
    payload: Omit<Keyword, "id" | "created_at" | "updated_at" | "next_run_at">,
  ) =>
    request<Keyword>(`/keywords/${id}`, {
      method: "PATCH",
      body: JSON.stringify(payload),
    }),
  deleteKeyword: (id: number) =>
    request<void>(`/keywords/${id}`, { method: "DELETE" }),
  items: (keywordId: number, filters: ItemFilters = {}) => {
    const params = new URLSearchParams({
      keyword_id: String(keywordId),
      limit: "200",
    });
    if (filters.source) params.set("source_id", filters.source);
    if (filters.language) params.set("language", filters.language);
    if (filters.sentiment) params.set("sentiment", filters.sentiment);
    if (filters.topic) params.set("topic", filters.topic);
    return request<{ total: number; items: Item[] }>(`/items?${params}`);
  },
  insightSummary: (keywordId: number, filters: ItemFilters = {}) => {
    const params = new URLSearchParams({
      keyword_id: String(keywordId),
      top_limit: "5",
    });
    if (filters.source) params.set("source_id", filters.source);
    if (filters.language) params.set("language", filters.language);
    if (filters.sentiment) params.set("sentiment", filters.sentiment);
    if (filters.topic) params.set("topic", filters.topic);
    return request<InsightSummary>(`/insights/summary?${params}`);
  },
  insightClusters: (keywordId: number, filters: ItemFilters = {}) => {
    const params = new URLSearchParams({
      keyword_id: String(keywordId),
      min_items: "2",
      limit: "10",
    });
    if (filters.source) params.set("source_id", filters.source);
    if (filters.language) params.set("language", filters.language);
    if (filters.sentiment) params.set("sentiment", filters.sentiment);
    if (filters.topic) params.set("topic", filters.topic);
    return request<TrendClusters>(`/insights/clusters?${params}`);
  },
  startRun: (keywordId: number, sourceIds?: string[]) =>
    request<Batch>("/runs", {
      method: "POST",
      body: JSON.stringify({
        keyword_id: keywordId,
        trigger: "manual",
        source_ids: sourceIds,
      }),
    }),
  runs: (keywordId: number) =>
    request<Batch[]>(`/runs?keyword_id=${keywordId}&limit=5`),
  run: (id: string) => request<Batch>(`/runs/${id}`),
  cancelRun: (id: string) =>
    request<{ id: string; state: string }>(`/runs/${id}/cancel`, {
      method: "POST",
    }),
  uploadSubtitleVideo: async (file: File, signal?: AbortSignal) => {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch(`${API_BASE}/subtitles/upload`, {
      method: "POST",
      body: formData,
      signal,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `Upload failed (${response.status})`);
    }
    return response.json() as Promise<SubtitleUploadResult>;
  },
  uploadSubtitleOverlay: async (file: File, signal?: AbortSignal) => {
    const formData = new FormData();
    formData.append("file", file);
    const response = await fetch(`${API_BASE}/subtitles/overlays`, {
      method: "POST",
      body: formData,
      signal,
    });
    if (!response.ok) {
      const body = await response.json().catch(() => ({}));
      throw new Error(body.detail || `Overlay upload failed (${response.status})`);
    }
    return response.json() as Promise<SubtitleOverlayUploadResult>;
  },
  subtitleVideoMetadata: (videoId: string, signal?: AbortSignal) =>
    request<MediaMetadata>(`/subtitles/video/${videoId}/metadata`, { signal }),
  parseSubtitleText: (text: string) =>
    request<SubtitleParseResult>("/subtitles/parse", {
      method: "POST",
      body: JSON.stringify({ text }),
    }),
  parseSubtitleTextV2: (
    text: string,
    mediaDurationMs?: number | null,
    signal?: AbortSignal,
  ) =>
    request<SubtitleParseResultV2>("/subtitles/v2/parse", {
      method: "POST",
      body: JSON.stringify({
        text,
        ...(mediaDurationMs ? { media_duration_ms: mediaDurationMs } : {}),
      }),
      signal,
    }),
  alignSubtitleDocument: (
    videoId: string,
    document: SubtitleParseResultV2["document"],
    options: SubtitleAlignmentOptions = {},
    cueIds?: string[] | null,
    signal?: AbortSignal,
  ) =>
    request<SubtitleJob>("/subtitles/v2/align", {
      method: "POST",
      body: JSON.stringify({
        video_id: videoId,
        document,
        options,
        ...(cueIds?.length ? { cue_ids: cueIds } : {}),
      }),
      signal,
    }),
  subtitleJob: (jobId: string, signal?: AbortSignal) =>
    request<SubtitleJob>(`/subtitles/jobs/${jobId}`, { signal }),
  cancelSubtitleJob: (jobId: string, signal?: AbortSignal) =>
    request<SubtitleJob>(`/subtitles/jobs/${jobId}/cancel`, {
      method: "POST",
      signal,
    }),
  renderSubtitleDocument: (
    videoId: string,
    document: SubtitleParseResultV2["document"],
    options: SubtitleRenderOptionsV2,
    overlay?: SubtitleOverlayRenderOptions | null,
    signal?: AbortSignal,
  ) =>
    request<SubtitleRenderJob>("/subtitles/v2/render", {
      method: "POST",
      body: JSON.stringify({
        video_id: videoId,
        document,
        options,
        ...(overlay ? { overlay } : {}),
      }),
      signal,
    }),
  subtitleRenderJob: (jobId: string, signal?: AbortSignal) =>
    request<SubtitleRenderJob>(`/subtitles/jobs/${jobId}`, { signal }),
  cancelSubtitleRenderJob: (jobId: string, signal?: AbortSignal) =>
    request<SubtitleRenderJob>(`/subtitles/jobs/${jobId}/cancel`, {
      method: "POST",
      signal,
    }),
  burnSubtitleVideo: (
    videoId: string,
    subtitles: SubtitleCueV2[] | SubtitleItem[],
    options: SubtitleBurnOptions,
  ) =>
    request<SubtitleBurnResult>("/subtitles/burn", {
      method: "POST",
      body: JSON.stringify({
        video_id: videoId,
        subtitles: subtitles.map((subtitle) =>
          "start_ms" in subtitle ? cueToLegacySubtitle(subtitle) : subtitle,
        ),
        options,
      }),
    }),
  deleteVideo: (id: string, type: string) =>
    request<void>(`/videos/${id}?type=${type}`, { method: "DELETE" }),
};
