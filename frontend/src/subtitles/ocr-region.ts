import type { SubtitleOcrRegion } from "./types";

export const DEFAULT_OCR_REGION: SubtitleOcrRegion = {
  x: 10,
  y: 72,
  width: 80,
  height: 18,
};

export const MIN_OCR_DIMENSION = 2; // minimum 2% width/height

export type OcrContentBox = {
  left: number;
  top: number;
  width: number;
  height: number;
};

/** Pixel bounds of the video image inside an object-fit: contain frame. */
export const fitOcrContentBox = (
  frameWidth: number,
  frameHeight: number,
  videoWidth: number,
  videoHeight: number,
): OcrContentBox => {
  const safeWidth = Math.max(1, frameWidth);
  const safeHeight = Math.max(1, frameHeight);
  const aspect = videoWidth > 0 && videoHeight > 0
    ? videoWidth / videoHeight
    : safeWidth / safeHeight;
  const frameAspect = safeWidth / safeHeight;
  const width = frameAspect > aspect ? safeHeight * aspect : safeWidth;
  const height = frameAspect > aspect ? safeHeight : safeWidth / aspect;
  return {
    left: (safeWidth - width) / 2,
    top: (safeHeight - height) / 2,
    width: Math.max(1, width),
    height: Math.max(1, height),
  };
};

export const clampOcrRegion = (region: Partial<SubtitleOcrRegion>): SubtitleOcrRegion => {
  const width = Math.max(
    MIN_OCR_DIMENSION,
    Math.min(100, typeof region.width === "number" && Number.isFinite(region.width) ? region.width : DEFAULT_OCR_REGION.width),
  );
  const height = Math.max(
    MIN_OCR_DIMENSION,
    Math.min(100, typeof region.height === "number" && Number.isFinite(region.height) ? region.height : DEFAULT_OCR_REGION.height),
  );
  const x = Math.max(
    0,
    Math.min(
      100 - width,
      typeof region.x === "number" && Number.isFinite(region.x) ? region.x : DEFAULT_OCR_REGION.x,
    ),
  );
  const y = Math.max(
    0,
    Math.min(
      100 - height,
      typeof region.y === "number" && Number.isFinite(region.y) ? region.y : DEFAULT_OCR_REGION.y,
    ),
  );

  return {
    x: Math.round(x * 100) / 100,
    y: Math.round(y * 100) / 100,
    width: Math.round(width * 100) / 100,
    height: Math.round(height * 100) / 100,
  };
};
