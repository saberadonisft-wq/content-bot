import type { OverlayLayout } from "./types";

export const DEFAULT_OVERLAY_LAYOUT: OverlayLayout = {
  x: 14,
  y: 12,
  width: 22,
};

type OverlayFitBox = {
  frameWidth: number;
  frameHeight: number;
  imageWidth: number;
  imageHeight: number;
};

const clamp = (value: number, minimum: number, maximum: number) =>
  Math.max(minimum, Math.min(maximum, value));

const finiteOr = (value: unknown, fallback: number) =>
  typeof value === "number" && Number.isFinite(value) ? value : fallback;

const roundTenths = (value: number) => Math.round(value * 10) / 10;

export const normalizeOverlayLayout = (
  value: unknown,
  fallback: OverlayLayout = DEFAULT_OVERLAY_LAYOUT,
): OverlayLayout => {
  const candidate =
    typeof value === "object" && value !== null
      ? (value as Partial<OverlayLayout>)
      : {};
  return {
    x: roundTenths(clamp(finiteOr(candidate.x, fallback.x), 0, 100)),
    y: roundTenths(clamp(finiteOr(candidate.y, fallback.y), 0, 100)),
    width: roundTenths(clamp(finiteOr(candidate.width, fallback.width), 4, 90)),
  };
};

export const fitOverlayLayout = (
  value: OverlayLayout,
  box: OverlayFitBox,
): OverlayLayout => {
  const normalized = normalizeOverlayLayout(value);
  const frameWidth = Math.max(1, box.frameWidth);
  const frameHeight = Math.max(1, box.frameHeight);
  const imageWidth = Math.max(1, box.imageWidth);
  const imageHeight = Math.max(1, box.imageHeight);
  const frameAspect = frameWidth / frameHeight;
  const imageAspect = imageWidth / imageHeight;
  const maximumWidthFromHeight = (90 * imageAspect) / frameAspect;
  const maximumWidth = Math.max(1, Math.min(90, maximumWidthFromHeight));
  const minimumWidth = Math.min(4, maximumWidth);
  const width = clamp(normalized.width, minimumWidth, maximumWidth);
  const height = (width * frameAspect) / imageAspect;
  const halfWidth = width / 2;
  const halfHeight = height / 2;
  return {
    x: roundTenths(clamp(normalized.x, halfWidth, 100 - halfWidth)),
    y: roundTenths(clamp(normalized.y, halfHeight, 100 - halfHeight)),
    width: roundTenths(width),
  };
};
