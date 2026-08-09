import {
  DEFAULT_FRAME_TIMING,
  type FrameTiming,
  type SubtitleCueV2,
} from "./types";

const TIMECODE_PATTERN =
  /^\s*(?:(\d{1,4}):)?(\d{1,2}):(\d{2})(?:[.,](\d{1,3}))?\s*$/;

export const clampMs = (value: number, minimum = 0, maximum = Infinity) =>
  Math.min(maximum, Math.max(minimum, Math.round(value)));

export const secondsToMs = (seconds: number) =>
  Number.isFinite(seconds) ? Math.max(0, Math.round(seconds * 1000)) : 0;

export const msToSeconds = (milliseconds: number) =>
  Math.max(0, Math.round(milliseconds)) / 1000;

export const formatTimecode = (
  milliseconds: number,
  separator: "." | "," = ".",
) => {
  const safeMs = Math.max(0, Math.round(milliseconds));
  const hours = Math.floor(safeMs / 3_600_000);
  const minutes = Math.floor((safeMs % 3_600_000) / 60_000);
  const seconds = Math.floor((safeMs % 60_000) / 1000);
  const millis = safeMs % 1000;
  return `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}${separator}${String(millis).padStart(3, "0")}`;
};

export const formatCompactTimecode = (milliseconds: number) => {
  const safeMs = Math.max(0, Math.round(milliseconds));
  const hours = Math.floor(safeMs / 3_600_000);
  const minutes = Math.floor((safeMs % 3_600_000) / 60_000);
  const seconds = Math.floor((safeMs % 60_000) / 1000);
  const millis = safeMs % 1000;
  const prefix = hours > 0 ? `${hours}:${String(minutes).padStart(2, "0")}` : `${minutes}`;
  return `${prefix}:${String(seconds).padStart(2, "0")}.${String(millis).padStart(3, "0")}`;
};

export const parseTimecode = (value: string): number | null => {
  const match = TIMECODE_PATTERN.exec(value);
  if (!match) return null;
  const hasHours = match[1] !== undefined;
  const hours = hasHours ? Number(match[1]) : 0;
  const minutes = Number(match[2]);
  const seconds = Number(match[3]);
  if ((hasHours && minutes >= 60) || seconds >= 60) return null;
  const millis = Number((match[4] ?? "").padEnd(3, "0"));
  return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis;
};

const normalizedFrameTiming = (timing: FrameTiming | number): FrameTiming => {
  if (typeof timing === "number") {
    return Number.isFinite(timing) && timing > 0
      ? {
          frame_rate_numerator: timing,
          frame_rate_denominator: 1,
          is_vfr: false,
          frame_pts_ms: [],
        }
      : DEFAULT_FRAME_TIMING;
  }
  return timing.frame_rate_numerator > 0 && timing.frame_rate_denominator > 0
    ? timing
    : DEFAULT_FRAME_TIMING;
};

export const framesPerSecond = (timing: FrameTiming | number) => {
  const normalized = normalizedFrameTiming(timing);
  return normalized.frame_rate_numerator / normalized.frame_rate_denominator;
};

export const frameDurationMs = (timing: FrameTiming | number) =>
  1000 / framesPerSecond(timing);

const lowerBound = (values: readonly number[], target: number) => {
  let low = 0;
  let high = values.length;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (values[middle] < target) low = middle + 1;
    else high = middle;
  }
  return low;
};

export const snapMsToFrame = (
  milliseconds: number,
  timing: FrameTiming | number,
) => {
  const targetMs = Math.max(0, Math.round(milliseconds));
  const normalized = normalizedFrameTiming(timing);
  const points = normalized.is_vfr ? normalized.frame_pts_ms : [];
  if (points.length > 0) {
    const rightIndex = lowerBound(points, targetMs);
    if (rightIndex <= 0) return points[0];
    if (rightIndex >= points.length) return points[points.length - 1];
    const left = points[rightIndex - 1];
    const right = points[rightIndex];
    return targetMs - left <= right - targetMs ? left : right;
  }
  const frameIndex = Math.round(
    (targetMs * normalized.frame_rate_numerator) /
      (1000 * normalized.frame_rate_denominator),
  );
  return Math.max(
    0,
    Math.round(
      (frameIndex * 1000 * normalized.frame_rate_denominator) /
        normalized.frame_rate_numerator,
    ),
  );
};

export const adjacentFrameMs = (
  milliseconds: number,
  direction: -1 | 1,
  timing: FrameTiming | number,
) => {
  const targetMs = Math.max(0, Math.round(milliseconds));
  const normalized = normalizedFrameTiming(timing);
  const points = normalized.is_vfr ? normalized.frame_pts_ms : [];
  if (points.length > 0) {
    const insertion = lowerBound(points, targetMs);
    const index =
      direction > 0
        ? insertion < points.length && points[insertion] === targetMs
          ? insertion + 1
          : insertion
        : insertion < points.length && points[insertion] === targetMs
          ? insertion - 1
          : insertion - 1;
    return points[Math.max(0, Math.min(points.length - 1, index))];
  }

  const scaled =
    (targetMs * normalized.frame_rate_numerator) /
    (1000 * normalized.frame_rate_denominator);
  const snapped = snapMsToFrame(targetMs, normalized);
  const currentIndex = Math.round(scaled);
  const nextIndex =
    targetMs === snapped
      ? currentIndex + direction
      : direction > 0
        ? Math.ceil(scaled)
        : Math.floor(scaled);
  return Math.max(
    0,
    Math.round(
      (Math.max(0, nextIndex) * 1000 * normalized.frame_rate_denominator) /
        normalized.frame_rate_numerator,
    ),
  );
};

export const keyboardStepMs = (
  event: Pick<KeyboardEvent, "altKey" | "ctrlKey" | "metaKey" | "shiftKey">,
  framesPerSecond: number,
) => {
  if (event.altKey) return Math.max(1, Math.round(frameDurationMs(framesPerSecond)));
  if (event.ctrlKey || event.metaKey) return 100;
  if (event.shiftKey) return 10;
  return 1;
};

export const keyboardTargetMs = (
  milliseconds: number,
  direction: -1 | 1,
  event: Pick<KeyboardEvent, "altKey" | "ctrlKey" | "metaKey" | "shiftKey">,
  timing: FrameTiming | number,
) =>
  event.altKey
    ? adjacentFrameMs(milliseconds, direction, timing)
    : milliseconds + direction * keyboardStepMs(event, framesPerSecond(timing));

export const sortCues = (cues: readonly SubtitleCueV2[]) =>
  [...cues].sort(
    (left, right) =>
      left.start_ms - right.start_ms ||
      left.end_ms - right.end_ms ||
      left.id.localeCompare(right.id),
  );

export const findOverlapCueIds = (cues: readonly SubtitleCueV2[]) => {
  const warnings = new Set<string>();
  let owner: SubtitleCueV2 | null = null;
  for (const cue of cues) {
    if (owner && cue.start_ms < owner.end_ms) {
      warnings.add(owner.id);
      warnings.add(cue.id);
    }
    if (!owner || cue.end_ms > owner.end_ms) owner = cue;
  }
  return warnings;
};

export const findActiveCue = (
  sortedCues: readonly SubtitleCueV2[],
  currentMs: number,
): SubtitleCueV2 | null =>
  new SubtitleIntervalIndex(sortedCues, true).findActive(currentMs);

export const cuesInTimeRange = (
  sortedCues: readonly SubtitleCueV2[],
  rangeStartMs: number,
  rangeEndMs: number,
) => new SubtitleIntervalIndex(sortedCues, true).inRange(rangeStartMs, rangeEndMs);

export class SubtitleIntervalIndex {
  readonly cues: readonly SubtitleCueV2[];
  private readonly prefixMaximumEnd: number[];

  constructor(cues: readonly SubtitleCueV2[], alreadySorted = false) {
    this.cues = alreadySorted ? cues : sortCues(cues);
    let maximumEnd = 0;
    this.prefixMaximumEnd = this.cues.map((cue) => {
      maximumEnd = Math.max(maximumEnd, cue.end_ms);
      return maximumEnd;
    });
  }

  findActive(currentMs: number): SubtitleCueV2 | null {
    let low = 0;
    let high = this.cues.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (this.cues[middle].start_ms <= currentMs) low = middle + 1;
      else high = middle;
    }

    for (let index = low - 1; index >= 0; index -= 1) {
      if (this.prefixMaximumEnd[index] <= currentMs) break;
      const cue = this.cues[index];
      if (currentMs >= cue.start_ms && currentMs < cue.end_ms) return cue;
    }
    return null;
  }

  inRange(rangeStartMs: number, rangeEndMs: number) {
    let low = 0;
    let high = this.prefixMaximumEnd.length;
    while (low < high) {
      const middle = (low + high) >> 1;
      if (this.prefixMaximumEnd[middle] < rangeStartMs) low = middle + 1;
      else high = middle;
    }

    const visible: SubtitleCueV2[] = [];
    for (let index = low; index < this.cues.length; index += 1) {
      const cue = this.cues[index];
      if (cue.start_ms > rangeEndMs) break;
      if (cue.end_ms >= rangeStartMs) visible.push(cue);
    }
    return visible;
  }
}

export const rulerStepMs = (pixelsPerSecond: number) => {
  if (pixelsPerSecond >= 150) return 500;
  if (pixelsPerSecond >= 100) return 1000;
  if (pixelsPerSecond >= 60) return 2000;
  if (pixelsPerSecond >= 28) return 5000;
  return 10_000;
};
