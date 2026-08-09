export const hexToRgba = (hex: string, alpha: number) => {
  let color = hex.replace("#", "");
  if (color.length === 3) color = color.split("").map((character) => character + character).join("");
  const numeric = Number.parseInt(color, 16) || 0;
  const safeAlpha = Math.max(0, Math.min(1, alpha));
  return `rgba(${(numeric >> 16) & 255}, ${(numeric >> 8) & 255}, ${numeric & 255}, ${safeAlpha})`;
};
