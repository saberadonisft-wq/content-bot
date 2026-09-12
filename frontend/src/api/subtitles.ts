import { cueToLegacySubtitle } from "../subtitles/model";
import type {
  MediaMetadata,
  SubtitleCueV2,
  SubtitleMaskRegion,
  SubtitleParseResultV2
} from "../subtitles/types";
import { API_BASE, getAuthHeader, request } from "../transport/client";
import type { GeminiApiStatus, GeminiModelsResponse, GeminiSubtitleJob, GeminiSubtitleOptions, SubtitleAlignmentOptions, SubtitleAssPreviewResult, SubtitleBurnOptions, SubtitleBurnResult, SubtitleItem, SubtitleJob, SubtitleOverlayRenderOptions, SubtitleOverlayUploadResult, SubtitleParseResult, SubtitleRenderJob, SubtitleRenderOptionsV2, SubtitleUploadResult } from "./types";
export const uploadSubtitleVideo = async (file: File, signal?: AbortSignal) => {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(`${API_BASE}/subtitles/upload`, {
    method: "POST",
    headers: getAuthHeader(),
    body: formData,
    signal,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Upload failed (${response.status})`);
  }
  return response.json() as Promise<SubtitleUploadResult>;
};

export const uploadSubtitleOverlay = async (file: File, signal?: AbortSignal) => {
  const formData = new FormData();
  formData.append("file", file);
  const response = await fetch(`${API_BASE}/subtitles/overlays`, {
    method: "POST",
    headers: getAuthHeader(),
    body: formData,
    signal,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `Overlay upload failed (${response.status})`);
  }
  return response.json() as Promise<SubtitleOverlayUploadResult>;
};

export const subtitleVideoMetadata = (videoId: string, signal?: AbortSignal) =>
  request<MediaMetadata>(`/subtitles/video/${videoId}/metadata`, { signal });

export const parseSubtitleText = (text: string) =>
  request<SubtitleParseResult>("/subtitles/parse", {
    method: "POST",
    body: JSON.stringify({ text }),
  });

export const parseSubtitleTextV2 = (
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
  });

export const alignSubtitleDocument = (
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
  });

export const generateSubtitlesWithGemini = (
  videoId: string,
  options: GeminiSubtitleOptions = {},
  signal?: AbortSignal,
) =>
  request<GeminiSubtitleJob>("/subtitles/v2/generate/gemini", {
    method: "POST",
    body: JSON.stringify({ video_id: videoId, options }),
    signal,
  });

export const geminiApiStatus = (signal?: AbortSignal) =>
  request<GeminiApiStatus>("/subtitles/gemini/status", { signal });

export const geminiModels = (signal?: AbortSignal) =>
  request<GeminiModelsResponse>("/subtitles/gemini/models", { signal });

export const geminiSubtitleJob = (jobId: string, signal?: AbortSignal) =>
  request<GeminiSubtitleJob>(`/subtitles/gemini/jobs/${jobId}`, { signal });

export const cancelGeminiSubtitleJob = (jobId: string, signal?: AbortSignal) =>
  request<GeminiSubtitleJob>(`/subtitles/gemini/jobs/${jobId}/cancel`, {
    method: "POST",
    signal,
  });

export const subtitleJob = (jobId: string, signal?: AbortSignal) =>
  request<SubtitleJob>(`/subtitles/jobs/${jobId}`, { signal });

export const cancelSubtitleJob = (jobId: string, signal?: AbortSignal) =>
  request<SubtitleJob>(`/subtitles/jobs/${jobId}/cancel`, {
    method: "POST",
    signal,
  });

export const renderSubtitleDocument = (
  videoId: string,
  document: SubtitleParseResultV2["document"],
  options: SubtitleRenderOptionsV2,
  overlay?: SubtitleOverlayRenderOptions | null,
  masks?: readonly SubtitleMaskRegion[],
  signal?: AbortSignal,
  voice?: { project_id: string; revision: number } | null,
) =>
  request<SubtitleRenderJob>("/subtitles/v2/render", {
    method: "POST",
    body: JSON.stringify({
      video_id: videoId,
      document,
      options,
      ...(overlay ? { overlay } : {}),
      ...(masks?.length ? { masks } : {}),
      ...(voice ? { voice_project_id: voice.project_id, voice_revision: voice.revision } : {}),
    }),
    signal,
  });

export const previewSubtitleDocument = (
  videoId: string,
  document: SubtitleParseResultV2["document"],
  options: SubtitleRenderOptionsV2,
  signal?: AbortSignal,
) =>
  request<SubtitleAssPreviewResult>("/subtitles/v2/preview-ass", {
    method: "POST",
    body: JSON.stringify({ video_id: videoId, document, options }),
    signal,
  });

export const subtitleRenderJob = (jobId: string, signal?: AbortSignal) =>
  request<SubtitleRenderJob>(`/subtitles/jobs/${jobId}`, { signal });

export const cancelSubtitleRenderJob = (jobId: string, signal?: AbortSignal) =>
  request<SubtitleRenderJob>(`/subtitles/jobs/${jobId}/cancel`, {
    method: "POST",
    signal,
  });

export const burnSubtitleVideo = (
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
  });