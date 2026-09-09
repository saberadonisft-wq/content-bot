import type { SubtitleBurnOptions, SubtitleCueV2 } from "../api";
import { normalizeOverlayLayout } from "./overlay";
import { normalizeSubtitleMasks } from "./masks";
import type { OverlayLayout, SubtitleMaskRegion, VideoClip } from "./types";
import { normalizeVideoClips } from "./video-clips";

export type SavedSubtitleDraft = {
  version: 2;
  videoId: string | null;
  projectName: string;
  mediaDurationMs: number;
  rawText: string;
  cues: SubtitleCueV2[];
  selectedCueId: string | null;
  activeGeminiJobId: string | null;
  activeAlignmentJobId: string | null;
  activeRenderJobId: string | null;
  options: SubtitleBurnOptions;
  overlayId: string | null;
  overlayName: string;
  overlayLayout: OverlayLayout;
  subtitleMasks: SubtitleMaskRegion[];
  videoClips: VideoClip[];
};

type UnknownRecord = Record<string, unknown>;

const isRecord = (value: unknown): value is UnknownRecord =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const finiteNumber = (value: unknown): value is number =>
  typeof value === "number" && Number.isFinite(value);

const integerMs = (value: unknown) =>
  typeof value === "number" && Number.isSafeInteger(value) ? value : null;

const secondsAsMs = (value: unknown) =>
  finiteNumber(value) && value >= 0 ? Math.round(value * 1000) : null;

const timingSources = new Set<SubtitleCueV2["timing_source"]>([
  "manual",
  "gemini_estimate",
  "asr_word",
  "forced_alignment",
  "imported_srt",
  "imported_vtt",
]);

const normalizeCue = (
  candidate: unknown,
  index: number,
  usedIds: Set<string>,
): SubtitleCueV2 | null => {
  if (!isRecord(candidate)) return null;
  const startMs =
    integerMs(candidate.start_ms) ?? secondsAsMs(candidate.start_seconds);
  const endMs = integerMs(candidate.end_ms) ?? secondsAsMs(candidate.end_seconds);
  const text = typeof candidate.text === "string" ? candidate.text.trim() : "";
  if (startMs === null || endMs === null || startMs < 0 || endMs <= startMs || !text) {
    return null;
  }
  const rawId =
    typeof candidate.id === "string" && candidate.id.trim()
      ? candidate.id.trim()
      : `draft-${index + 1}-${startMs}`;
  let id = rawId;
  let suffix = 2;
  while (usedIds.has(id)) {
    id = `${rawId}-${suffix}`;
    suffix += 1;
  }
  usedIds.add(id);
  const timingSource = timingSources.has(
    candidate.timing_source as SubtitleCueV2["timing_source"],
  )
    ? (candidate.timing_source as SubtitleCueV2["timing_source"])
    : "manual";
  const timingPrecision = integerMs(candidate.timing_precision_ms);
  const confidence = finiteNumber(candidate.confidence)
    ? Math.max(0, Math.min(1, candidate.confidence))
    : null;
  return {
    id,
    start_ms: startMs,
    end_ms: endMs,
    text: text.slice(0, 4000),
    ...(isRecord(candidate.layout) && finiteNumber(candidate.layout.x) && finiteNumber(candidate.layout.y)
      ? { layout: { x: Math.max(0, Math.min(100, candidate.layout.x)), y: Math.max(0, Math.min(100, candidate.layout.y)) } }
      : {}),
    ...(typeof candidate.secondary_text === "string" && candidate.secondary_text.trim()
      ? { secondary_text: candidate.secondary_text.trim().slice(0, 4000) }
      : {}),
    timing_source: timingSource,
    timing_precision_ms:
      timingPrecision !== null && timingPrecision >= 1 ? timingPrecision : 1,
    confidence,
    needs_review: candidate.needs_review === true,
    revision:
      integerMs(candidate.revision) !== null && Number(candidate.revision) >= 0
        ? Number(candidate.revision)
        : 0,
  };
};

const normalizeOptions = (
  candidate: unknown,
  defaults: SubtitleBurnOptions,
): SubtitleBurnOptions => {
  if (!isRecord(candidate)) return { ...defaults };
  const next = { ...defaults } as UnknownRecord;
  for (const [key, defaultValue] of Object.entries(defaults)) {
    const value = candidate[key];
    if (typeof defaultValue === "boolean" && typeof value === "boolean") next[key] = value;
    else if (typeof defaultValue === "number" && finiteNumber(value)) next[key] = value;
    else if (typeof defaultValue === "string" && typeof value === "string") next[key] = value;
    else if (defaultValue === null && (value === null || finiteNumber(value))) next[key] = value;
  }
  return next as SubtitleBurnOptions;
};

export const normalizeSavedDraft = (
  value: unknown,
  defaultOptions: SubtitleBurnOptions,
): SavedSubtitleDraft | null => {
  if (!isRecord(value)) return null;
  const candidates = Array.isArray(value.cues)
    ? value.cues
    : Array.isArray(value.subtitles)
      ? value.subtitles
      : null;
  if (!candidates) return null;
  const usedIds = new Set<string>();
  const cues = candidates
    .map((candidate, index) => normalizeCue(candidate, index, usedIds))
    .filter((cue): cue is SubtitleCueV2 => cue !== null)
    .sort(
      (left, right) =>
        left.start_ms - right.start_ms ||
        left.end_ms - right.end_ms ||
        left.id.localeCompare(right.id),
    );
  const selectedCueId =
    typeof value.selectedCueId === "string" && cues.some((cue) => cue.id === value.selectedCueId)
      ? value.selectedCueId
      : null;
  const videoId =
    typeof value.videoId === "string" && /^[a-f0-9]{12,32}$/.test(value.videoId)
      ? value.videoId
      : null;
  return {
    version: 2,
    videoId,
    projectName:
      typeof value.projectName === "string" && value.projectName.trim()
        ? value.projectName.trim().slice(0, 160)
        : "Dự án đã lưu",
    mediaDurationMs:
      integerMs(value.mediaDurationMs) !== null && Number(value.mediaDurationMs) >= 0
        ? Number(value.mediaDurationMs)
        : 0,
    rawText: typeof value.rawText === "string" ? value.rawText.slice(0, 500_000) : "",
    cues,
    selectedCueId,
    activeGeminiJobId:
      typeof value.activeGeminiJobId === "string" ? value.activeGeminiJobId : null,
    activeAlignmentJobId:
      typeof value.activeAlignmentJobId === "string" ? value.activeAlignmentJobId : null,
    activeRenderJobId:
      typeof value.activeRenderJobId === "string" ? value.activeRenderJobId : null,
    options: normalizeOptions(value.options, defaultOptions),
    overlayId:
      typeof value.overlayId === "string" && /^[a-f0-9]{64}$/.test(value.overlayId)
        ? value.overlayId
        : null,
    overlayName:
      typeof value.overlayName === "string"
        ? value.overlayName.trim().slice(0, 255)
        : "",
    overlayLayout: normalizeOverlayLayout(value.overlayLayout),
    subtitleMasks: normalizeSubtitleMasks(value.subtitleMasks),
    videoClips: normalizeVideoClips(value.videoClips, Number(value.mediaDurationMs) || 0),
  };
};

export const readSavedDraft = (
  storage: Pick<Storage, "getItem">,
  key: string,
  defaultOptions: SubtitleBurnOptions,
) => {
  try {
    const raw = storage.getItem(key);
    return raw ? normalizeSavedDraft(JSON.parse(raw), defaultOptions) : null;
  } catch {
    return null;
  }
};
