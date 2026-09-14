import { cueToLegacySubtitle } from "../subtitles/model";
import type {
  MediaMetadata,
  SubtitleCueV2,
  SubtitleDocumentV2,
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

export type GeminiReviewScope = { combined?: boolean; mode: "all" | "range" | "selected" | "long" | "timing"; start_ms?: number; end_ms?: number; cue_ids?: string[]; region_ids?: string[] };
export type TimingReviewRegion = { id: string; start_ms: number; end_ms: number; cue_ids: string[]; locked_count: number; reasons: string[] };
export const scanSubtitleTiming = (videoId: string, document: SubtitleDocumentV2) =>
  request<{ regions: TimingReviewRegion[] }>("/subtitles/v2/review/timing-scan", { method: "POST", body: JSON.stringify({ video_id: videoId, document }) });
export type GeminiReviewProposal = {
  id: string; operation: string; issue: string; start_ms: number; end_ms: number;
  before: SubtitleCueV2[]; after: SubtitleCueV2[]; cue_ids: string[];
  reason: string; evidence: string; certainty: string; source_revision: number;
  state: "pending" | "applied" | "skipped" | "conflict"; locked: boolean;
};
export type GeminiReview = { id: string; video_id: string; state: string; model: string; proposals: GeminiReviewProposal[]; snapshot: SubtitleDocumentV2; can_undo?: boolean; warnings: { code: string; message: string }[] };
export type GeminiReviewJob = Omit<SubtitleJob, "result"> & { result?: GeminiReview | null };
export const createGeminiReview = (videoId: string, document: SubtitleDocumentV2, scope: GeminiReviewScope, model?: string) =>
  request<{ review: GeminiReview; job: GeminiReviewJob }>("/subtitles/v2/review/gemini", { method: "POST", body: JSON.stringify({ video_id: videoId, document, scope, model }) });
export const getGeminiReview = (id: string, signal?: AbortSignal) => request<GeminiReview>(`/subtitles/gemini/reviews/${id}`, { signal });
export const getGeminiReviewJob = (id: string, signal?: AbortSignal) => request<GeminiReviewJob>(`/subtitles/gemini/jobs/${id}`, { signal });
export const resumeGeminiReview = (id: string) => request<GeminiReviewJob>(`/subtitles/gemini/reviews/${id}/resume`, { method: "POST" });
export const applyGeminiReview = (id: string, document: SubtitleDocumentV2, proposalIds: string[], skip = false) =>
  request<{ document: SubtitleDocumentV2; review: GeminiReview; applied_ids: string[]; can_undo: boolean }>(`/subtitles/gemini/reviews/${id}/apply`, { method: "POST", body: JSON.stringify({ document, proposal_ids: proposalIds, skip }) });
export const undoGeminiReview = (id: string, document: SubtitleDocumentV2) =>
  request<{ document: SubtitleDocumentV2; review: GeminiReview; can_undo: boolean }>(`/subtitles/gemini/reviews/${id}/undo`, { method: "POST", body: JSON.stringify({ document }) });

export type SubtitleVersion = { id: string; name: string; source: string; model: string | null; created_at: string; cue_count: number; document: SubtitleDocumentV2 };
export const listSubtitleVersions = (videoId: string, offset = 0, signal?: AbortSignal) => request<{ versions: Omit<SubtitleVersion, "document">[]; total: number }>(`/subtitles/videos/${videoId}/versions?offset=${offset}&limit=20`, { signal });
export const getSubtitleVersion = (videoId: string, id: string) => request<SubtitleVersion>(`/subtitles/videos/${videoId}/versions/${id}`);
export const saveSubtitleVersion = (videoId: string, document: SubtitleDocumentV2, name = "Bản đang chỉnh") => request<SubtitleVersion>(`/subtitles/videos/${videoId}/versions`, { method: "POST", body: JSON.stringify({ document, name }) });

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
