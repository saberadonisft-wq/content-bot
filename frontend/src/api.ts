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

export const api = {
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
};
