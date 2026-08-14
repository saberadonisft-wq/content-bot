export const SUBTITLE_DESIGN_HEIGHT = 720;

export type PreviewSubtitleMetrics = {
  scale: number;
  fontSize: number;
  outlineWidth: number;
  shadowWidth: number;
  shadowBlur: number;
  paddingX: number;
  paddingY: number;
};

export function previewSubtitleMetrics(
  frameHeight: number,
  fontSize: number,
  outlineWidth: number,
  shadowWidth: number,
): PreviewSubtitleMetrics {
  const scale = Math.max(0, frameHeight) / SUBTITLE_DESIGN_HEIGHT;
  return {
    scale,
    fontSize: fontSize * scale,
    outlineWidth: outlineWidth * scale,
    shadowWidth: shadowWidth * scale,
    shadowBlur: 4 * scale,
    paddingX: 12 * scale,
    paddingY: 4 * scale,
  };
}
