import { API_BASE, request } from "../transport/client";
import type { CrawlerLoginStatus, Keyword, LiveWallConfig, Source, TikTokOAuthStatus, VideoLibraryItem } from "./types";
import type { VideoDownloadJob, VideoDownloadQuality } from "./types";
export const videos = () => request<VideoLibraryItem[]>("/videos");
export const videoDownloads = () => request<VideoDownloadJob[]>("/videos/downloads");
export const downloadVideos = (urls: string[], quality: VideoDownloadQuality, cookieText?: string) =>
  request<VideoDownloadJob[]>("/videos/downloads", {
    method: "POST", body: JSON.stringify({ urls, quality, cookie_text: cookieText || undefined }),
  });
export const cancelVideoDownload = (id: string) => request<VideoDownloadJob>(
  `/videos/downloads/${encodeURIComponent(id)}/cancel`, { method: "POST" },
);
export const retryVideoDownload = (id: string, cookieText?: string) => request<VideoDownloadJob>(
  `/videos/downloads/${encodeURIComponent(id)}/retry`, {
    method: "POST", body: JSON.stringify({ cookie_text: cookieText || undefined }),
  },
);

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
