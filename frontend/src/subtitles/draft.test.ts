import { describe, expect, it } from "vitest";
import type { SubtitleBurnOptions } from "../api";
import { normalizeSavedDraft } from "./draft";

const options = {
  font_name: "Arimo",
  font_size: 38,
  animation: "none",
  trim_end: null,
} as SubtitleBurnOptions;

describe("subtitle draft migration", () => {
  it("migrates legacy seconds while dropping corrupt cues and duplicate IDs", () => {
    const draft = normalizeSavedDraft(
      {
        subtitles: [
          { id: "same", start_seconds: 1.001, end_seconds: 2.501, text: "Một" },
          { id: "same", start_seconds: 3, end_seconds: 4, text: "Hai" },
          { id: "broken", start_ms: 10, end_ms: 9, text: "Hỏng" },
        ],
        selectedCueId: "broken",
        options: { font_size: Number.POSITIVE_INFINITY, animation: "fade" },
        overlayId: "a".repeat(64),
        overlayName: "logo.png",
        overlayLayout: { x: -20, y: 47.25, width: 120 },
      },
      options,
    );

    expect(draft?.cues).toHaveLength(2);
    expect(draft?.cues[0]).toMatchObject({ start_ms: 1001, end_ms: 2501 });
    expect(draft?.cues[1].id).toBe("same-2");
    expect(draft?.selectedCueId).toBeNull();
    expect(draft?.options.font_size).toBe(38);
    expect(draft?.options.animation).toBe("fade");
    expect(draft?.overlayId).toBe("a".repeat(64));
    expect(draft?.overlayName).toBe("logo.png");
    expect(draft?.overlayLayout).toEqual({ x: 0, y: 47.3, width: 90 });
  });

  it("rejects values that are not draft objects", () => {
    expect(normalizeSavedDraft(null, options)).toBeNull();
    expect(normalizeSavedDraft({ version: 2 }, options)).toBeNull();
  });

  it("drops an unsafe overlay id while keeping the normalized layout", () => {
    const draft = normalizeSavedDraft(
      {
        cues: [{ id: "one", start_ms: 0, end_ms: 1000, text: "Một" }],
        overlayId: "../../logo.png",
        overlayName: "  logo.webp  ",
        overlayLayout: { x: 14, y: 12, width: 22 },
      },
      options,
    );

    expect(draft?.overlayId).toBeNull();
    expect(draft?.overlayName).toBe("logo.webp");
  });
});
