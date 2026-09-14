import type { SubtitleCueV2 } from "./types";

export function sentenceCount(text: string): number {
  const normalized = text.normalize("NFC").replace(/\s+/gu, " ").trim()
    .replace(/(?<=\d)[.,](?=\d)/gu, "")
    .replace(/\b(?:TS|ThS|PGS|GS|BS|TP|Mr|Mrs|Ms|Dr|Prof|St)\./giu, "")
    .replace(/\b(?:\p{L}\.){2,}/gu, "")
    .replace(/\.{2,}|…+/gu, " ");
  return normalized.split(/[.!?。！？]+/u).filter(part => /[\p{L}\p{N}]/u.test(part)).length;
}

export function isLongCue(cue: Pick<SubtitleCueV2, "text" | "start_ms" | "end_ms">): boolean {
  const normalized = cue.text.normalize("NFC").replace(/\s+/gu, " ").trim();
  return [...normalized].length > 84 || cue.end_ms - cue.start_ms > 6000
    || cue.text.trim().split(/\r?\n/u).length > 2 || sentenceCount(cue.text) > 1;
}
