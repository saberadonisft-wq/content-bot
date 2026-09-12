import { useEffect } from "react";
import type { SavedSubtitleDraft } from "./draft";

export const SUBTITLE_DRAFT_KEY = "content-bot:subtitle-studio:v2";

export function useDraftPersistence(draft: SavedSubtitleDraft) {
  useEffect(() => {
    const timer = setTimeout(() => {
      try {
        localStorage.setItem(SUBTITLE_DRAFT_KEY, JSON.stringify(draft));
      } catch {
        // Private mode or a full quota must not prevent editing.
      }
    }, 600);
    return () => clearTimeout(timer);
  }, [draft]);
}
