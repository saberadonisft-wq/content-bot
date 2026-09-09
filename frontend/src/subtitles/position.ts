import type { SubtitleCueV2, SubtitlePosition } from "./types";

export const applyCuePosition = (
  cues: readonly SubtitleCueV2[],
  cueId: string,
  position: SubtitlePosition,
  all: boolean,
): SubtitleCueV2[] => cues.map((cue) => all || cue.id === cueId
  ? { ...cue, layout: { ...position }, revision: cue.revision + 1 }
  : cue);
