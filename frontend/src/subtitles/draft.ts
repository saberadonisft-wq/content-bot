import type { SubtitleBurnOptions, SubtitleCueV2 } from "../api";
import { normalizeOverlayLayout } from "./overlay";
import { normalizeSubtitleMasks } from "./masks";
import type { ExtractionMode, OverlayLayout, SubtitleDocumentV2, SubtitleMaskRegion, SubtitleOcrRegion, VideoClip } from "./types";
import { clampOcrRegion, DEFAULT_OCR_REGION } from './ocr-region';
import type { PendingExtraction } from './useExtractionJobs';
import { normalizeVideoClips } from "./video-clips";

export type SavedSubtitleDraft = {
  version: 2;
  videoId: string | null;
  projectName: string;
  mediaDurationMs: number;
  rawText: string;
  cues: SubtitleCueV2[];
  documentMeta?: Partial<Omit<SubtitleDocumentV2, 'segments'>> & { revision: number; run_id: string | null };
  sourceDocument?: SubtitleDocumentV2 | null;
  translatedSourceRevision?: number | null;
  extractionSettings?: ExtractionSettings;
  pendingExtraction?: PendingExtraction | null;
  selectedCueId: string | null;
  activeGeminiJobId: string | null;
  lastGeminiJobId?: string | null;
  activeAlignmentJobId: string | null;
  activeRenderJobId: string | null;
  options: SubtitleBurnOptions;
  overlayId: string | null;
  overlayName: string;
  overlayLayout: OverlayLayout;
  subtitleMasks: SubtitleMaskRegion[];
  videoClips: VideoClip[];
};

export type ExtractionSettings = {
  mode: ExtractionMode; region: SubtitleOcrRegion; ocrLanguage: string; sampleFps: number; autoProbe?: boolean;
  asrLanguage: string; asrModel: string; asrDevice?: 'auto' | 'cpu' | 'cuda'; asrComputeType?: string;
  bilingual: boolean; model: string;
};
export const DEFAULT_EXTRACTION_SETTINGS: ExtractionSettings = {
  mode: 'ocr', region: DEFAULT_OCR_REGION, ocrLanguage: 'zh', sampleFps: 3, autoProbe: false,
  asrLanguage: 'auto', asrModel: 'small', asrDevice: 'auto', asrComputeType: 'auto', bilingual: false, model: '',
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
  "ocr",
  "asr",
]);

const normalizeCue = (
  candidate: unknown,
  index: number,
  usedIds: Set<string>,
  allowEmpty = false,
): SubtitleCueV2 | null => {
  if (!isRecord(candidate)) return null;
  const startMs =
    integerMs(candidate.start_ms) ?? secondsAsMs(candidate.start_seconds);
  const endMs = integerMs(candidate.end_ms) ?? secondsAsMs(candidate.end_seconds);
  const text = typeof candidate.text === "string" ? candidate.text.trim() : "";
  if (startMs === null || endMs === null || startMs < 0 || endMs <= startMs || (!text && !allowEmpty)) {
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
  const speechStart = integerMs(candidate.speech_start_ms), speechEnd = integerMs(candidate.speech_end_ms);
  const validSpeech = speechStart !== null && speechEnd !== null && speechStart >= 0 && speechEnd > speechStart;
  const evidence = candidate.speech_evidence;
  const validEvidence = validSpeech && isRecord(evidence)
    && ['asr_observed', 'ctc_aligned', 'energy_estimated', 'interpolated', 'manual'].includes(String(evidence.method))
    && (evidence.audio_identity === null || typeof evidence.audio_identity === 'string' && evidence.audio_identity.length <= 128)
    && typeof evidence.transcript_sha256 === 'string' && /^[a-f0-9]{64}$/.test(evidence.transcript_sha256)
    && evidence.start_ms === speechStart && evidence.end_ms === speechEnd
    && typeof evidence.algorithm === 'string' && evidence.algorithm.length > 0 && evidence.algorithm.length <= 100
    && typeof evidence.transcript_complete === 'boolean';
  return {
    id,
    start_ms: startMs,
    end_ms: endMs,
    text: text.slice(0, 4000),
    source_text: typeof candidate.source_text === "string" ? candidate.source_text.slice(0, 4000) : null,
    source_language: typeof candidate.source_language === "string" ? candidate.source_language.slice(0, 32) : null,
    content_source: ["audio", "screen", "mixed", "unknown"].includes(String(candidate.content_source)) ? candidate.content_source as SubtitleCueV2["content_source"] : "unknown",
    origin_chunk_id: typeof candidate.origin_chunk_id === "string" ? candidate.origin_chunk_id.slice(0, 64) : null,
    origin_model: typeof candidate.origin_model === "string" ? candidate.origin_model.slice(0, 128) : null,
    locked: candidate.locked === true,
    speech_start_ms: validSpeech ? speechStart : null,
    speech_end_ms: validSpeech ? speechEnd : null,
    speech_evidence: validEvidence ? evidence as SubtitleCueV2['speech_evidence'] : null,
    words: Array.isArray(candidate.words) ? candidate.words.filter((word): word is NonNullable<SubtitleCueV2["words"]>[number] =>
      isRecord(word) && typeof word.id === "string" && typeof word.text === "string"
      && integerMs(word.start_ms) !== null && integerMs(word.end_ms) !== null
      && Number(word.end_ms) > Number(word.start_ms)
      && ((Number(word.start_ms) >= startMs && Number(word.end_ms) <= endMs)
        || (validSpeech && Number(word.start_ms) >= speechStart && Number(word.end_ms) <= speechEnd)))
      .slice(0, 500).map(word => ({ ...word, alignment_method:
        ['asr_observed', 'ctc_aligned', 'energy_estimated', 'interpolated', 'manual'].includes(String(word.alignment_method))
          ? word.alignment_method : null })) : null,
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

export function normalizeSourceDocument(value: unknown): SubtitleDocumentV2 | null {
  if (!isRecord(value) || !Array.isArray(value.segments) || value.schema_version !== 2) return null;
  const used = new Set<string>();
  const segments = value.segments.map((cue, i) => normalizeCue(cue, i, used, true))
    .filter((cue): cue is SubtitleCueV2 => cue !== null)
    .map(cue => ({ ...cue, text: value.document_role === 'source' ? cue.text : cue.source_text ?? cue.text,
      source_text: value.document_role === 'source' ? cue.text : cue.source_text ?? cue.text }));
  // A corrupt source must never authorize applying an in-flight job to a different document.
  if (segments.length !== value.segments.length) return null;
  return { schema_version: 2, document_role: 'source', revision: Math.max(0, integerMs(value.revision) ?? 0),
    ...(typeof value.media_fingerprint === 'string' && value.media_fingerprint.length > 0 && value.media_fingerprint.length <= 128 ? { media_fingerprint: value.media_fingerprint } : {}),
    run_id: typeof value.run_id === 'string' ? value.run_id.slice(0, 64) : null,
    language: typeof value.language === 'string' && /^[A-Za-z0-9-]{2,32}$/.test(value.language) ? value.language : 'und',
    timebase: 'milliseconds', timing_source: timingSources.has(value.timing_source as SubtitleCueV2['timing_source'])
      ? value.timing_source as SubtitleCueV2['timing_source'] : 'manual',
    timing_precision_ms: Math.max(1, Math.min(60000, integerMs(value.timing_precision_ms) ?? 1)), segments };
}

function normalizeExtractionSettings(value: unknown): ExtractionSettings {
  if (!isRecord(value)) return DEFAULT_EXTRACTION_SETTINGS;
  const defaults = DEFAULT_EXTRACTION_SETTINGS;
  return { mode: ['ocr', 'asr', 'gemini'].includes(String(value.mode)) ? value.mode as ExtractionMode : defaults.mode,
    autoProbe: value.autoProbe === true,
    region: clampOcrRegion(isRecord(value.region) ? value.region : defaults.region),
    ocrLanguage: typeof value.ocrLanguage === 'string' ? value.ocrLanguage.slice(0, 32) : defaults.ocrLanguage,
    sampleFps: finiteNumber(value.sampleFps) ? Math.max(1, Math.min(30, value.sampleFps)) : defaults.sampleFps,
    asrLanguage: typeof value.asrLanguage === 'string' ? value.asrLanguage.slice(0, 32) : defaults.asrLanguage,
    asrModel: typeof value.asrModel === 'string' ? value.asrModel.slice(0, 64) : defaults.asrModel,
    asrDevice: ['auto', 'cpu', 'cuda'].includes(String(value.asrDevice)) ? value.asrDevice as 'auto' | 'cpu' | 'cuda' : 'auto',
    asrComputeType: typeof value.asrComputeType === 'string' ? value.asrComputeType.slice(0, 32) : 'auto',
    bilingual: value.bilingual === true, model: typeof value.model === 'string' ? value.model.slice(0, 128) : '' };
}

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
  const pending = value.pendingExtraction;
  const pendingExtraction = isRecord(pending) && pending.videoId === videoId && videoId
    && typeof pending.id === 'string' && /^[a-f0-9]{20}$/.test(pending.id)
    && typeof pending.snapshot === 'string' && /^[a-f0-9]{64}$/.test(pending.snapshot)
    && ['ocr', 'asr', 'translation'].includes(String(pending.kind))
    ? pending as PendingExtraction : null;
  return {
    version: 2,
    documentMeta: isRecord(value.documentMeta) ? { revision: Math.max(0, integerMs(value.documentMeta.revision) ?? 0), run_id: typeof value.documentMeta.run_id === "string" ? value.documentMeta.run_id.slice(0, 64) : null,
      ...(typeof value.documentMeta.media_fingerprint === 'string' && value.documentMeta.media_fingerprint.length > 0 && value.documentMeta.media_fingerprint.length <= 128 ? { media_fingerprint: value.documentMeta.media_fingerprint } : {}),
      ...(value.documentMeta.schema_version === 2 ? { schema_version: 2 as const } : {}),
      ...(value.documentMeta.timebase === 'milliseconds' ? { timebase: 'milliseconds' as const } : {}),
      ...(typeof value.documentMeta.language === 'string' && value.documentMeta.language.trim() ? { language: value.documentMeta.language.slice(0, 16) } : {}),
      ...(timingSources.has(value.documentMeta.timing_source as SubtitleCueV2['timing_source']) ? { timing_source: value.documentMeta.timing_source as SubtitleCueV2['timing_source'] } : {}),
      ...(integerMs(value.documentMeta.timing_precision_ms) !== null ? { timing_precision_ms: Math.max(1, integerMs(value.documentMeta.timing_precision_ms)!) } : {}),
      ...(['source', 'translation'].includes(String(value.documentMeta.document_role)) ? { document_role: value.documentMeta.document_role as 'source' | 'translation' } : {}),
      ...(integerMs(value.documentMeta.source_revision) !== null ? { source_revision: Math.max(0, Number(value.documentMeta.source_revision)) } : {}),
      ...(typeof value.documentMeta.source_run_id === 'string' ? { source_run_id: value.documentMeta.source_run_id.slice(0, 64) } : {}),
      ...(Array.isArray(value.documentMeta.translation_models) ? { translation_models: value.documentMeta.translation_models.filter((model): model is string => typeof model === 'string' && model.length <= 128).slice(0, 10) } : {}),
    } : { revision: 0, run_id: null },
    sourceDocument: normalizeSourceDocument(value.sourceDocument),
    translatedSourceRevision: integerMs(value.translatedSourceRevision),
    extractionSettings: normalizeExtractionSettings(value.extractionSettings),
    pendingExtraction,
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
    lastGeminiJobId: typeof value.lastGeminiJobId === "string" && /^[a-f0-9]{20}$/.test(value.lastGeminiJobId) ? value.lastGeminiJobId : null,
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
