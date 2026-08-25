import type { VideoClip } from "./types";

export const MIN_VIDEO_CLIP_MS = 40;

export const createInitialVideoClip = (durationMs: number): VideoClip[] =>
  durationMs > 0
    ? [{ id: "video-1", start_ms: 0, end_ms: Math.round(durationMs) }]
    : [];

export const normalizeVideoClips = (
  value: unknown,
  durationMs: number,
): VideoClip[] => {
  if (!Array.isArray(value) || durationMs <= 0) return [];
  const usedIds = new Set<string>();
  const clips = value
    .map((candidate, index): VideoClip | null => {
      if (!candidate || typeof candidate !== "object") return null;
      const record = candidate as Record<string, unknown>;
      const startMs = Number(record.start_ms);
      const endMs = Number(record.end_ms);
      if (
        !Number.isSafeInteger(startMs) ||
        !Number.isSafeInteger(endMs) ||
        startMs < 0 ||
        endMs <= startMs ||
        startMs >= durationMs
      ) {
        return null;
      }
      const baseId =
        typeof record.id === "string" && record.id.trim()
          ? record.id.trim().slice(0, 80)
          : `video-${index + 1}`;
      let id = baseId;
      let suffix = 2;
      while (usedIds.has(id)) id = `${baseId}-${suffix++}`;
      usedIds.add(id);
      return { id, start_ms: startMs, end_ms: Math.min(endMs, durationMs) };
    })
    .filter((clip): clip is VideoClip => clip !== null)
    .sort((left, right) => left.start_ms - right.start_ms || left.end_ms - right.end_ms);

  return clips.filter(
    (clip, index) => index === 0 || clip.start_ms >= clips[index - 1].end_ms,
  );
};

export const splitVideoClip = (
  clips: readonly VideoClip[],
  clipId: string,
  splitMs: number,
): VideoClip[] => {
  const rounded = Math.round(splitMs);
  return clips.flatMap((clip) => {
    if (
      clip.id !== clipId ||
      rounded - clip.start_ms < MIN_VIDEO_CLIP_MS ||
      clip.end_ms - rounded < MIN_VIDEO_CLIP_MS
    ) {
      return [clip];
    }
    return [
      { ...clip, end_ms: rounded },
      { id: `${clip.id}-${rounded}`, start_ms: rounded, end_ms: clip.end_ms },
    ];
  });
};

export const findVideoClipAt = (
  clips: readonly VideoClip[],
  milliseconds: number,
) => clips.find(
  (clip) => milliseconds >= clip.start_ms && milliseconds < clip.end_ms,
) ?? null;

export const keptVideoDurationMs = (clips: readonly VideoClip[]) =>
  clips.reduce((total, clip) => total + clip.end_ms - clip.start_ms, 0);

export const deletedVideoRanges = (
  clips: readonly VideoClip[],
  durationMs: number,
) => {
  const ranges: Array<{ start_ms: number; end_ms: number }> = [];
  let cursorMs = 0;
  for (const clip of clips) {
    if (clip.start_ms > cursorMs) {
      ranges.push({ start_ms: cursorMs, end_ms: clip.start_ms });
    }
    cursorMs = Math.max(cursorMs, clip.end_ms);
  }
  if (cursorMs < durationMs) {
    ranges.push({ start_ms: cursorMs, end_ms: durationMs });
  }
  return ranges;
};

export const nearestKeptVideoTimeMs = (
  clips: readonly VideoClip[],
  milliseconds: number,
) => {
  if (clips.length === 0) return 0;
  const targetMs = Math.round(milliseconds);
  if (findVideoClipAt(clips, targetMs)) return targetMs;
  const previous = [...clips].reverse().find((clip) => clip.end_ms <= targetMs);
  const next = clips.find((clip) => clip.start_ms > targetMs);
  if (!previous) return next?.start_ms ?? clips[0].start_ms;
  if (!next) return Math.max(previous.start_ms, previous.end_ms - 1);
  return targetMs - previous.end_ms <= next.start_ms - targetMs
    ? Math.max(previous.start_ms, previous.end_ms - 1)
    : next.start_ms;
};
