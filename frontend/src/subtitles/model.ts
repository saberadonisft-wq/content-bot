import type { SubtitleCueV2, SubtitleTimingSource } from "./types";
import { clampMs, formatTimecode, sortCues } from "./time";

export const TIMING_SOURCE_LABELS: Record<SubtitleTimingSource, string> = {
  manual: "Manual",
  gemini_estimate: "Gemini estimate",
  asr_word: "ASR word",
  forced_alignment: "Aligned",
  imported_srt: "Imported SRT",
  imported_vtt: "Imported VTT",
};

export const speechEvidenceLabel = (cue: SubtitleCueV2): string => {
  const method = cue.speech_evidence?.method;
  if (!method) return cue.timing_source === 'forced_alignment' ? 'Mốc căn cũ · chưa rõ phương pháp' : '';
  return { asr_observed: 'Mốc lời từ ASR', ctc_aligned: 'Mốc lời căn âm học',
    energy_estimated: 'Mốc lời ước lượng từ năng lượng', interpolated: 'Mốc lời nội suy',
    manual: 'Mốc lời chỉnh tay' }[method];
};

const newCueId = () => {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `cue-${crypto.randomUUID()}`;
  }
  return `cue-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
};

const splitTextNearRatio = (text: string, ratio: number): [string, string] | null => {
  const normalized = text.trim();
  if (normalized.length < 2) return null;
  const target = Math.max(1, Math.min(normalized.length - 1, Math.round(normalized.length * ratio)));
  const spaces = Array.from(normalized.matchAll(/\s+/g), (match) => match.index ?? 0)
    .filter((index) => index > 0 && index < normalized.length - 1);
  const splitAt = spaces.length > 0
    ? spaces.reduce((best, index) =>
        Math.abs(index - target) < Math.abs(best - target) ? index : best,
      )
    : target;
  const left = normalized.slice(0, splitAt).trim();
  const right = normalized.slice(splitAt).trim();
  return left && right ? [left, right] : null;
};

const manualCue = (cue: SubtitleCueV2, revision: number): SubtitleCueV2 => ({
  ...cue,
  speech_start_ms: null,
  speech_end_ms: null,
  speech_evidence: null,
  words: null,
  timing_source: "manual",
  timing_precision_ms: 1,
  confidence: null,
  needs_review: false,
  revision,
});

export const createManualCue = (
  cues: readonly SubtitleCueV2[],
  durationMs: number,
): SubtitleCueV2 => {
  const lastEnd = cues.reduce((maximum, cue) => Math.max(maximum, cue.end_ms), 0);
  const projectEnd = Math.max(1, durationMs || lastEnd + 3000);
  const startMs = Math.min(lastEnd, Math.max(0, projectEnd - 1));
  const endMs = Math.min(projectEnd, Math.max(startMs + 1, startMs + 3000));
  return {
    id: newCueId(),
    start_ms: startMs,
    end_ms: endMs,
    text: "Dòng phụ đề mới",
    timing_source: "manual",
    timing_precision_ms: 1,
    confidence: null,
    needs_review: false,
    revision: 0,
  };
};

export const updateCueById = (
  cues: readonly SubtitleCueV2[],
  cueId: string,
  patch: Partial<Pick<SubtitleCueV2, "start_ms" | "end_ms" | "text" | "secondary_text">>,
  options: { markManual?: boolean; durationMs?: number } = {},
) => {
  const next = cues.map((cue) => {
    if (cue.id !== cueId) return cue;
    const maximumStart =
      options.durationMs && Number.isFinite(options.durationMs)
        ? Math.max(0, options.durationMs - 1)
        : Infinity;
    const startMs = clampMs(
      patch.start_ms ?? cue.start_ms,
      0,
      maximumStart,
    );
    const endMs = clampMs(
      patch.end_ms ?? cue.end_ms,
      startMs + 1,
      Math.max(startMs + 1, options.durationMs ?? Infinity),
    );
    const markManual = options.markManual ?? true;
    const invalidatesAlignment =
      markManual &&
      ("start_ms" in patch || "end_ms" in patch || "text" in patch);
    return {
      ...cue,
      ...patch,
      start_ms: startMs,
      end_ms: endMs,
      timing_source: markManual ? ("manual" as const) : cue.timing_source,
      timing_precision_ms: markManual ? 1 : cue.timing_precision_ms,
      ...(invalidatesAlignment
        ? {
            speech_start_ms: null,
            speech_end_ms: null,
            speech_evidence: null,
            words: null,
            confidence: null,
            needs_review: false,
          }
        : {}),
      revision: markManual ? cue.revision + 1 : cue.revision,
    };
  });
  return sortCues(next);
};

export const splitCueById = (
  cues: readonly SubtitleCueV2[],
  cueId: string,
  requestedSplitMs: number,
) => {
  const cue = cues.find((item) => item.id === cueId);
  if (!cue || cue.end_ms - cue.start_ms < 2) return [...cues];
  const splitMs = clampMs(requestedSplitMs, cue.start_ms + 1, cue.end_ms - 1);
  const ratio = (splitMs - cue.start_ms) / (cue.end_ms - cue.start_ms);
  const textParts = splitTextNearRatio(cue.text, ratio);
  if (!textParts) return [...cues];
  const secondaryParts = cue.secondary_text
    ? splitTextNearRatio(cue.secondary_text, ratio)
    : null;
  const revision = cue.revision + 1;
  const first = manualCue(
    {
      ...cue,
      end_ms: splitMs,
      text: textParts[0],
      secondary_text: secondaryParts?.[0] ?? null,
    },
    revision,
  );
  const second = manualCue(
    {
      ...cue,
      id: newCueId(),
      start_ms: splitMs,
      text: textParts[1],
      secondary_text: secondaryParts?.[1] ?? null,
    },
    revision,
  );
  return sortCues(cues.flatMap((item) => (item.id === cueId ? [first, second] : [item])));
};

export const mergeCueWithNext = (
  cues: readonly SubtitleCueV2[],
  cueId: string,
) => {
  const sorted = sortCues(cues);
  const index = sorted.findIndex((cue) => cue.id === cueId);
  if (index < 0 || index >= sorted.length - 1) return [...cues];
  const first = sorted[index];
  const second = sorted[index + 1];
  const merged = manualCue(
    {
      ...first,
      start_ms: Math.min(first.start_ms, second.start_ms),
      end_ms: Math.max(first.end_ms, second.end_ms),
      text: `${first.text.trim()} ${second.text.trim()}`.trim(),
      secondary_text:
        first.secondary_text || second.secondary_text
          ? `${first.secondary_text?.trim() ?? ""} ${second.secondary_text?.trim() ?? ""}`.trim()
          : null,
    },
    Math.max(first.revision, second.revision) + 1,
  );
  return sortCues([
    ...sorted.slice(0, index),
    merged,
    ...sorted.slice(index + 2),
  ]);
};

export const cueToLegacySubtitle = (cue: SubtitleCueV2) => ({
  start_time: formatTimecode(cue.start_ms, ","),
  end_time: formatTimecode(cue.end_ms, ","),
  start_seconds: cue.start_ms / 1000,
  end_seconds: cue.end_ms / 1000,
  text: cue.text,
  ...(cue.secondary_text ? { secondary_text: cue.secondary_text } : {}),
});
