import { API_BASE, request } from "../transport/client";
import type { CrawlerLoginStatus, Keyword, LiveWallConfig, Source, TikTokOAuthStatus, VideoLibraryItem } from "./types";
export const videos = () => request<VideoLibraryItem[]>("/videos");

export const sources = () => request<Source[]>("/sources");

export const tiktokOAuthStatus = () =>
  request<TikTokOAuthStatus>("/auth/tiktok/status");

export const tiktokOAuthStartUrl = (username: string) =>
  `${API_BASE}/auth/tiktok/start?${new URLSearchParams({ username })}`;

export const refreshTikTokOAuth = () =>
  request<TikTokOAuthStatus>("/auth/tiktok/refresh", { method: "POST" });

export const disconnectTikTokOAuth = () =>
  request<void>("/auth/tiktok/connection", { method: "DELETE" });

export const crawlerLoginStatus = (sourceId: string) =>
  request<CrawlerLoginStatus>(
    `/crawler/profiles/${encodeURIComponent(sourceId)}/login`,
  );

export const startCrawlerLogin = (sourceId: string, timeoutSeconds = 1200) =>
  request<CrawlerLoginStatus>(
    `/crawler/profiles/${encodeURIComponent(sourceId)}/login`,
    {
      method: "POST",
      body: JSON.stringify({ timeout_seconds: timeoutSeconds }),
    },
  );

export const stopCrawlerLogin = (sourceId: string) =>
  request<CrawlerLoginStatus>(
    `/crawler/profiles/${encodeURIComponent(sourceId)}/login`,
    { method: "DELETE" },
  );

export const updateLiveWall = (keywordId: number, config: LiveWallConfig) =>
  request<Keyword>(`/keywords/${keywordId}/live-wall`, {
    method: "PUT",
    body: JSON.stringify(config),
  });

export const openInCoccoc = (keywordId: number, channelIds: string[]) =>
  request<{ opened: boolean; channel_count: number }>("/live-wall/open-browser", {
    method: "POST",
    body: JSON.stringify({ keyword_id: keywordId, channel_ids: channelIds }),
  });

export const deleteVideo = (id: string, type: string) =>
  request<void>(`/videos/${id}?type=${type}`, { method: "DELETE" });