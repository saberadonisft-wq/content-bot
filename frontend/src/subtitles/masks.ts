import type {
  SubtitleMaskEffect,
  SubtitleMaskRegion,
  SubtitleMaskShape,
} from "./types";

export const MAX_SUBTITLE_MASKS = 8;

export const DEFAULT_SUBTITLE_MASK: Omit<SubtitleMaskRegion, "id"> = {
  shape: "rectangle",
  effect: "blur",
  x: 20,
  y: 72,
  width: 60,
  height: 12,
  strength: 22,
  opacity: 1,
  feather: 2,
  cornerRadius: 14,
  color: "#000000",
};

const MASK_SHAPES = new Set<SubtitleMaskShape>([
  "rectangle",
  "rounded",
  "ellipse",
  "band",
]);
const MASK_EFFECTS = new Set<SubtitleMaskEffect>([
  "blur",
  "pixelate",
  "solid",
  "darken",
]);
const HEX_COLOR = /^#[0-9a-f]{6}$/i;

const finite = (value: unknown, fallback: number) =>
  typeof value === "number" && Number.isFinite(value) ? value : fallback;

const clamp = (value: number, minimum: number, maximum: number) =>
  Math.max(minimum, Math.min(maximum, value));

const round = (value: number) => Math.round(value * 100) / 100;

const createMaskId = () => {
  if (typeof crypto !== "undefined" && "randomUUID" in crypto) {
    return `mask-${crypto.randomUUID()}`;
  }
  return `mask-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
};

export const normalizeSubtitleMask = (
  candidate: Partial<SubtitleMaskRegion> & Pick<SubtitleMaskRegion, "id">,
): SubtitleMaskRegion => {
  const shape = MASK_SHAPES.has(candidate.shape as SubtitleMaskShape)
    ? (candidate.shape as SubtitleMaskShape)
    : DEFAULT_SUBTITLE_MASK.shape;
  const effect = MASK_EFFECTS.has(candidate.effect as SubtitleMaskEffect)
    ? (candidate.effect as SubtitleMaskEffect)
    : DEFAULT_SUBTITLE_MASK.effect;
  const width = shape === "band"
    ? 100
    : clamp(finite(candidate.width, DEFAULT_SUBTITLE_MASK.width), 4, 100);
  const height = clamp(finite(candidate.height, DEFAULT_SUBTITLE_MASK.height), 3, 100);
  const x = shape === "band"
    ? 0
    : clamp(finite(candidate.x, DEFAULT_SUBTITLE_MASK.x), 0, 100 - width);
  const y = clamp(finite(candidate.y, DEFAULT_SUBTITLE_MASK.y), 0, 100 - height);
  return {
    id: String(candidate.id || createMaskId()).slice(0, 80),
    shape,
    effect,
    x: round(x),
    y: round(y),
    width: round(width),
    height: round(height),
    strength: round(clamp(finite(candidate.strength, DEFAULT_SUBTITLE_MASK.strength), 1, 40)),
    opacity: round(clamp(finite(candidate.opacity, DEFAULT_SUBTITLE_MASK.opacity), 0.05, 1)),
    feather: round(clamp(finite(candidate.feather, DEFAULT_SUBTITLE_MASK.feather), 0, 20)),
    cornerRadius: round(clamp(finite(candidate.cornerRadius, DEFAULT_SUBTITLE_MASK.cornerRadius), 0, 50)),
    color: typeof candidate.color === "string" && HEX_COLOR.test(candidate.color)
      ? candidate.color.toUpperCase()
      : DEFAULT_SUBTITLE_MASK.color,
  };
};

export const createSubtitleMask = (
  overrides: Partial<Omit<SubtitleMaskRegion, "id">> = {},
) => normalizeSubtitleMask({ id: createMaskId(), ...DEFAULT_SUBTITLE_MASK, ...overrides });

export const normalizeSubtitleMasks = (candidate: unknown): SubtitleMaskRegion[] => {
  if (!Array.isArray(candidate)) return [];
  const usedIds = new Set<string>();
  const result: SubtitleMaskRegion[] = [];
  for (const item of candidate) {
    if (result.length >= MAX_SUBTITLE_MASKS) break;
    if (!item || typeof item !== "object") continue;
    const raw = item as Partial<SubtitleMaskRegion>;
    const rawId = typeof raw.id === "string" && /^[A-Za-z0-9_-]{1,80}$/.test(raw.id)
      ? raw.id
      : createMaskId();
    let id = rawId;
    let suffix = 2;
    while (usedIds.has(id)) id = `${rawId.slice(0, 74)}-${suffix++}`;
    usedIds.add(id);
    result.push(normalizeSubtitleMask({ ...raw, id }));
  }
  return result;
};
