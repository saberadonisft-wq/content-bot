import { cueToLegacySubtitle } from "../subtitles/model";
import type {
  MediaMetadata,
  SubtitleCueV2,
  SubtitleDocumentV2,
  SubtitleMaskRegion,
  SubtitleOcrRegion,
  SubtitleParseResultV2
} from "../subtitles/types";
import { API_BASE, getAuthHeader, request } from "../transport/client";
import type {
  GeminiApiStatus,
  GeminiModelsResponse,
  GeminiSubtitleJob,
  GeminiSubtitleOptions,
  SubtitleAlignmentOptions,
  SubtitleAsrOptions,
  SubtitleAsrRuntimeStatus,
  SubtitleAssPreviewResult,
  SubtitleBurnOptions,
  SubtitleBurnResult,
  SubtitleItem,
  SubtitleJob,
  SubtitleOcrOptions,
  SubtitleOverlayRenderOptions,
  SubtitleOverlayUploadResult,
  SubtitleParseResult,
  SubtitleRenderJob,
  SubtitleRenderOptionsV2,
  SubtitleSceneJob,
  SubtitleSceneChunk,
  SubtitleTranslateOptions,
  SubtitleUploadResult,
} from "./types";
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
      ...(cueIds != null ? { cue_ids: cueIds } : {}),
    }),
    signal,
  });

export const generateSubtitlesWithGemini = (
  videoId: string,
  options: GeminiSubtitleOptions = {},
  signal?: AbortSignal,
  currentDocument?: SubtitleDocumentV2,
  regenerate = false,
) =>
  request<GeminiSubtitleJob>("/subtitles/v2/generate/gemini", {
    method: "POST",
    body: JSON.stringify({ video_id: videoId, options, current_document: currentDocument, regenerate }),
    signal,
  });

export const geminiApiStatus = (signal?: AbortSignal) =>
  request<GeminiApiStatus>("/subtitles/gemini/status", { signal });

export const geminiModels = (signal?: AbortSignal) =>
  request<GeminiModelsResponse>("/subtitles/gemini/models", { signal });

export const geminiSubtitleJob = (jobId: string, signal?: AbortSignal) =>
  request<GeminiSubtitleJob>(`/subtitles/gemini/jobs/${jobId}`, { signal });
export const resumeGeminiSubtitleJob = (jobId: string, signal?: AbortSignal) =>
  request<GeminiSubtitleJob>(`/subtitles/gemini/jobs/${jobId}/resume`, { method: "POST", signal });

export type SubtitleVersion = { id: string; name: string; source: string; model: string | null; created_at: string; cue_count: number; document: SubtitleDocumentV2; media_fingerprint?: string | null; media_binding?: 'match' | 'mismatch' | 'unverified' };
export const listSubtitleVersions = (videoId: string, offset = 0, signal?: AbortSignal) => request<{ versions: Omit<SubtitleVersion, "document">[]; total: number; unreadable_count?: number }>(`/subtitles/videos/${videoId}/versions?offset=${offset}&limit=20`, { signal });
export const getSubtitleVersion = (videoId: string, id: string) => request<SubtitleVersion>(`/subtitles/videos/${videoId}/versions/${id}`);
export const saveSubtitleVersion = (videoId: string, document: SubtitleDocumentV2, name = "Bản đang chỉnh", signal?: AbortSignal, preserveSource = false) => request<SubtitleVersion>(`/subtitles/videos/${videoId}/versions`, { method: "POST", body: JSON.stringify({ document, name, preserve_source: preserveSource }), signal });

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

export type SubtitleSceneDetectOptions = {
  threshold?: number;
  min_scene_len_s?: number;
  target_duration_s?: number;
  min_duration_s?: number;
  max_duration_s?: number;
};

export const detectSubtitleScenes = (
  videoId: string,
  options: SubtitleSceneDetectOptions = {},
  signal?: AbortSignal,
) =>
  request<SubtitleSceneJob>("/subtitles/v2/scene-detect", {
    method: "POST",
    body: JSON.stringify({ video_id: videoId, ...options }),
    signal,
  });

export const exportSubtitleScenes = (
  videoId: string,
  sourceFingerprint: string,
  chunks: SubtitleSceneChunk[],
  document?: SubtitleDocumentV2 | null,
  signal?: AbortSignal,
) =>
  request<SubtitleSceneJob>("/subtitles/v2/scene-export", {
    method: "POST",
    body: JSON.stringify({
      video_id: videoId,
      source_fingerprint: sourceFingerprint,
      chunks: chunks.map(({ id, start_ms, end_ms }) => ({ id, start_ms, end_ms })),
      ...(document ? { document } : {}),
    }),
    signal,
  });

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

export const extractSubtitlesOcr = (
  videoId: string,
  region?: SubtitleOcrRegion | null,
  options?: SubtitleOcrOptions,
  signal?: AbortSignal,
) =>
  request<SubtitleJob>("/subtitles/v2/extract/ocr", {
    method: "POST",
    body: JSON.stringify({
      video_id: videoId,
      ...(region ? { region } : {}),
      ...(options?.source_language ? { source_language: options.source_language } : {}),
      ...(options?.sample_fps != null ? { sample_fps: options.sample_fps } : {}),
      ...(options?.min_duration_ms != null ? { min_duration_ms: options.min_duration_ms } : {}),
      ...(options?.max_gap_ms != null ? { max_gap_ms: options.max_gap_ms } : {}),
      ...(options?.auto_probe != null ? { auto_probe: options.auto_probe } : {}),
    }),
    signal,
  });

export const subtitleAsrRuntimeStatus = (signal?: AbortSignal) =>
  request<SubtitleAsrRuntimeStatus>('/subtitles/v2/extract/asr/status', { signal });

export const extractSubtitlesAsr = (
  videoId: string,
  options?: SubtitleAsrOptions,
  signal?: AbortSignal,
) =>
  request<SubtitleJob>("/subtitles/v2/extract/asr", {
    method: "POST",
    body: JSON.stringify({
      video_id: videoId,
      ...(options?.source_language ? { source_language: options.source_language } : {}),
      ...(options?.model ? { model: options.model } : {}),
      ...(options?.device ? { device: options.device } : {}),
      ...(options?.compute_type ? { compute_type: options.compute_type } : {}),
    }),
    signal,
  });

export const translateSubtitlesWithGemini = (
  videoId: string,
  document: SubtitleDocumentV2,
  options?: SubtitleTranslateOptions,
  cueIds?: string[] | null,
  signal?: AbortSignal,
) =>
  request<SubtitleJob>("/subtitles/v2/translate/gemini", {
    method: "POST",
    body: JSON.stringify({
      video_id: videoId,
      document,
      ...(options?.target_language ? { target_language: options.target_language } : {}),
      ...(options?.bilingual != null ? { bilingual: options.bilingual } : {}),
      ...(options?.model ? { model: options.model } : {}),
      ...(options?.batch_size != null ? { batch_size: options.batch_size } : {}),
      ...(cueIds ? { cue_ids: cueIds } : {}),
    }),
    signal,
  });

export const exportSourceSrt = (
  document: SubtitleDocumentV2,
  videoId?: string,
  filename?: string,
  signal?: AbortSignal,
) =>
  request<{ srt: string; count: number }>("/subtitles/v2/export/source-srt", {
    method: "POST",
    body: JSON.stringify({
      document,
      video_id: videoId,
      filename,
    }),
    signal,
  });
