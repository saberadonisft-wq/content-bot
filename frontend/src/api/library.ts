import { API_BASE, request } from "../transport/client";
import type { AcquisitionCandidatesPage, AcquisitionCapabilities, AcquisitionChannel, AcquisitionMode, AcquisitionRun, AcquisitionSelection, CrawlerLoginStatus, Keyword, LiveWallConfig, Source, TikTokOAuthStatus, VideoLibraryItem } from "./types";
import type { VideoDownloadJob, VideoDownloadQuality } from "./types";

export type ThumbnailSelectionResult = {
  thumbnail_path: string;
  best_timestamp_s: number;
  score: number;
  metrics?: Record<string, number>;
  thumbnail_url: string;
};

export const videos = () => request<VideoLibraryItem[]>("/videos");
export const videoDownloads = () => request<VideoDownloadJob[]>("/videos/downloads");
export const downloadVideos = (
  urls: string[],
  quality: VideoDownloadQuality,
  cookieText?: string,
  connectionId?: string,
) =>
  request<VideoDownloadJob[]>("/videos/downloads", {
    method: "POST", body: JSON.stringify({
      urls,
      quality,
      cookie_text: cookieText || undefined,
      connection_id: connectionId?.trim() || undefined,
    }),
  });
export const cancelVideoDownload = (id: string) => request<VideoDownloadJob>(
  `/videos/downloads/${encodeURIComponent(id)}/cancel`, { method: "POST" },
);
export const pauseVideoDownload = (id: string) => request<VideoDownloadJob>(
  `/videos/downloads/${encodeURIComponent(id)}/pause`, { method: "POST" },
);
export const retryVideoDownload = (id: string, cookieText?: string, connectionId?: string) => request<VideoDownloadJob>(
  `/videos/downloads/${encodeURIComponent(id)}/retry`, {
    method: "POST", body: JSON.stringify({
      cookie_text: cookieText || undefined,
      connection_id: connectionId?.trim() || undefined,
    }),
  },
);
export const resumeVideoDownload = (id: string, cookieText?: string, connectionId?: string) => request<VideoDownloadJob>(
  `/videos/downloads/${encodeURIComponent(id)}/resume`, {
    method: "POST", body: JSON.stringify({
      cookie_text: cookieText || undefined,
      connection_id: connectionId?.trim() || undefined,
    }),
  },
);

export const createAcquisitionRun = (payload: {
  mode: AcquisitionMode;
  targets?: string[];
  query?: string;
  source_id?: string;
  provider_id?: string;
  connection_id?: string;
  limits?: { max_candidates?: number; max_pages?: number; deadline_seconds?: number };
  filters?: Record<string, string | number | boolean | null>;
}) => request<AcquisitionRun>("/acquisition/runs", {
  method: "POST", body: JSON.stringify(payload),
});

export const acquisitionCapabilities = () =>
  request<AcquisitionCapabilities>("/acquisition/capabilities");

export const acquisitionRun = (id: string) =>
  request<AcquisitionRun>(`/acquisition/runs/${encodeURIComponent(id)}`);

export const acquisitionRuns = () =>
  request<{ items: AcquisitionRun[]; limit: number }>("/acquisition/runs?limit=50");

export const acquisitionCandidates = (id: string, options?: { onlyNotDownloaded?: boolean; offset?: number }) => {
  const params = new URLSearchParams({ limit: "100", offset: String(options?.offset ?? 0) });
  if (options?.onlyNotDownloaded) params.set("only_not_downloaded", "true");
  return request<AcquisitionCandidatesPage>(
    `/acquisition/runs/${encodeURIComponent(id)}/candidates?${params.toString()}`,
  );
};

export const cancelAcquisitionRun = (id: string) =>
  request<AcquisitionRun>(`/acquisition/runs/${encodeURIComponent(id)}/cancel`, { method: "POST" });

export const pauseAcquisitionRun = (id: string) =>
  request<AcquisitionRun>(`/acquisition/runs/${encodeURIComponent(id)}/pause`, { method: "POST" });

export const resumeAcquisitionRun = (id: string) =>
  request<AcquisitionRun>(`/acquisition/runs/${encodeURIComponent(id)}/resume`, { method: "POST" });

export const continueAcquisitionRun = (id: string, generation: number) =>
  request<AcquisitionRun>(`/acquisition/runs/${encodeURIComponent(id)}/continue`, {
    method: "POST", body: JSON.stringify({ expected_generation: generation }),
  });

export const selectAcquisitionDownloads = (payload: {
  candidate_ids: string[];
  quality: VideoDownloadQuality;
  idempotency_key?: string;
  cookie_text?: string;
  connection_id?: string;
}) => request<AcquisitionSelection>("/acquisition/download-selections", {
  method: "POST", body: JSON.stringify(payload),
});

export const acquisitionSelection = (id: string) =>
  request<AcquisitionSelection>(`/acquisition/download-selections/${encodeURIComponent(id)}`);

export const acquisitionSelections = () =>
  request<{ items: AcquisitionSelection[]; limit: number }>("/acquisition/download-selections?limit=50");

export const pauseAcquisitionSelection = (id: string) =>
  request<AcquisitionSelection>(`/acquisition/download-selections/${encodeURIComponent(id)}/pause`, { method: "POST" });

export const resumeAcquisitionSelection = (id: string, cookieText?: string) =>
  request<AcquisitionSelection>(`/acquisition/download-selections/${encodeURIComponent(id)}/resume`, {
    method: "POST", body: JSON.stringify({ cookie_text: cookieText || undefined }),
  });

export const cancelAcquisitionSelection = (id: string) =>
  request<AcquisitionSelection>(`/acquisition/download-selections/${encodeURIComponent(id)}/cancel`, { method: "POST" });

export const acquisitionChannels = () =>
  request<{ items: AcquisitionChannel[]; limit: number }>("/acquisition/channels");

export const saveAcquisitionChannel = (
  url: string,
  label = "",
  connectionId?: string,
) =>
  request<AcquisitionChannel>("/acquisition/channels", {
    method: "POST", body: JSON.stringify({
      url,
      label,
      connection_id: connectionId?.trim() || undefined,
    }),
  });

export const updateAcquisitionChannel = (
  id: string,
  patch: { label?: string; connection_id?: string | null },
) => request<AcquisitionChannel>(`/acquisition/channels/${encodeURIComponent(id)}`, {
  method: "PATCH", body: JSON.stringify(patch),
});

export const deleteAcquisitionChannel = (id: string) =>
  request<void>(`/acquisition/channels/${encodeURIComponent(id)}`, { method: "DELETE" });

export const updateAcquisitionSubscription = (
  id: string,
  subscription: AcquisitionChannel["subscription"],
) => request<AcquisitionChannel>(`/acquisition/channels/${encodeURIComponent(id)}/subscription`, {
  method: "PUT", body: JSON.stringify(subscription),
});

export const sources = () => request<Source[]>("/sources");

export const tiktokOAuthStatus = () =>
  request<TikTokOAuthStatus>("/auth/tiktok/status");

export const tiktokOAuthStartUrl = (username: string) =>
  `${API_BASE}/auth/tiktok/start?${new URLSearchParams({ username })}`;

export const refreshTikTokOAuth = () =>
  request<TikTokOAuthStatus>("/auth/tiktok/refresh", { method: "POST" });

export const disconnectTikTokOAuth = () =>
  request<void>("/auth/tiktok/connection", { method: "DELETE" });

const crawlerConnectionQuery = (connectionId: string) =>
  connectionId && connectionId !== "default"
    ? `?${new URLSearchParams({ connection_id: connectionId }).toString()}`
    : "";

export const crawlerLoginStatus = (sourceId: string, connectionId = "default") =>
  request<CrawlerLoginStatus>(
    `/crawler/profiles/${encodeURIComponent(sourceId)}/login${crawlerConnectionQuery(connectionId)}`,
  );

export const startCrawlerLogin = (
  sourceId: string,
  timeoutSeconds = 1200,
  connectionId = "default",
) =>
  request<CrawlerLoginStatus>(
    `/crawler/profiles/${encodeURIComponent(sourceId)}/login${crawlerConnectionQuery(connectionId)}`,
    {
      method: "POST",
      body: JSON.stringify({ timeout_seconds: timeoutSeconds }),
    },
  );

export const stopCrawlerLogin = (sourceId: string, connectionId = "default") =>
  request<CrawlerLoginStatus>(
    `/crawler/profiles/${encodeURIComponent(sourceId)}/login${crawlerConnectionQuery(connectionId)}`,
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

export const selectVideoThumbnail = (id: string, options?: { nCandidates?: number; antiDuplicate?: boolean }) => {
  const params = new URLSearchParams({
    n_candidates: String(options?.nCandidates ?? 15),
    anti_duplicate: String(options?.antiDuplicate ?? false),
  });
  return request<ThumbnailSelectionResult>(
    `/videos/${encodeURIComponent(id)}/thumbnail/select?${params.toString()}`,
    { method: "POST" },
  );
};
