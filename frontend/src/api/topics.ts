import { request } from "../transport/client";
import type { Batch, InsightSummary, Item, ItemFilters, Keyword, TrendClusters } from "./types";

export type CaptionCleanResult = {
  original_text: string;
  cleaned_text: string;
  hashtags: string[];
  safe_filename: string;
  changed: boolean;
};

export type CaptionApplyResult = {
  item_id: number;
  caption_original: string;
  caption_edited: string;
  caption_edited_at: string;
};

export const cleanCaption = (text: string, signal?: AbortSignal) =>
  request<CaptionCleanResult>("/captions/clean", {
    method: "POST",
    body: JSON.stringify({ text }),
    signal,
  });

export const applyCaption = (
  itemId: number,
  payload: { original_text?: string; edited_text: string },
) =>
  request<CaptionApplyResult>(`/items/${itemId}/caption`, {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const keywords = () => request<Keyword[]>("/keywords");

export const createKeyword = (
  payload: Omit<Keyword, "id" | "created_at" | "updated_at" | "next_run_at" | "live_wall">,
) =>
  request<Keyword>("/keywords", {
    method: "POST",
    body: JSON.stringify(payload),
  });

export const updateKeyword = (
  id: number,
  payload: Omit<Keyword, "id" | "created_at" | "updated_at" | "next_run_at" | "live_wall">,
) =>
  request<Keyword>(`/keywords/${id}`, {
    method: "PATCH",
    body: JSON.stringify(payload),
  });

export const deleteKeyword = (id: number) =>
  request<void>(`/keywords/${id}`, { method: "DELETE" });

export const items = (keywordId: number, filters: ItemFilters = {}, signal?: AbortSignal) => {
  const params = new URLSearchParams({
    keyword_id: String(keywordId),
    limit: "200",
  });
  if (filters.source) params.set("source_id", filters.source);
  if (filters.session) params.set("session_id", filters.session);
  if (filters.language) params.set("language", filters.language);
  if (filters.sentiment) params.set("sentiment", filters.sentiment);
  if (filters.topic) params.set("topic", filters.topic);
  return request<{ total: number; items: Item[] }>(`/items?${params}`, { signal });
};

export const insightSummary = (keywordId: number, filters: ItemFilters = {}, signal?: AbortSignal) => {
  const params = new URLSearchParams({
    keyword_id: String(keywordId),
    top_limit: "5",
  });
  if (filters.source) params.set("source_id", filters.source);
  if (filters.session) params.set("session_id", filters.session);
  if (filters.language) params.set("language", filters.language);
  if (filters.sentiment) params.set("sentiment", filters.sentiment);
  if (filters.topic) params.set("topic", filters.topic);
  return request<InsightSummary>(`/insights/summary?${params}`, { signal });
};

export const insightClusters = (keywordId: number, filters: ItemFilters = {}, signal?: AbortSignal) => {
  const params = new URLSearchParams({
    keyword_id: String(keywordId),
    min_items: "2",
    limit: "10",
  });
  if (filters.source) params.set("source_id", filters.source);
  if (filters.session) params.set("session_id", filters.session);
  if (filters.language) params.set("language", filters.language);
  if (filters.sentiment) params.set("sentiment", filters.sentiment);
  if (filters.topic) params.set("topic", filters.topic);
  return request<TrendClusters>(`/insights/clusters?${params}`, { signal });
};

export const startRun = (keywordId: number, sourceIds?: string[], channelIds?: string[]) =>
  request<Batch>("/runs", {
    method: "POST",
    body: JSON.stringify({
      keyword_id: keywordId,
      trigger: "manual",
      source_ids: sourceIds,
      channel_ids: channelIds,
    }),
  });

export const runs = (keywordId: number, signal?: AbortSignal) =>
  request<Batch[]>(`/runs?keyword_id=${keywordId}&limit=5`, { signal });

export const run = (id: string, signal?: AbortSignal) => request<Batch>(`/runs/${id}`, { signal });

export const cancelRun = (id: string) =>
  request<{ id: string; state: string }>(`/runs/${id}/cancel`, {
    method: "POST",
  });
