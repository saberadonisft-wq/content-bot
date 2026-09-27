import { useEffect, useLayoutEffect, useRef } from "react";
import type { SavedSubtitleDraft } from "./draft";

export const SUBTITLE_DRAFT_KEY = "content-bot:subtitle-studio:v2";

export function useDraftPersistence(draft: SavedSubtitleDraft) {
  const latest = useRef(draft);
  const persisted = useRef<SavedSubtitleDraft | null>(null);
  const flush = () => {
    if (persisted.current === latest.current) return;
    try { localStorage.setItem(SUBTITLE_DRAFT_KEY, JSON.stringify(latest.current)); persisted.current = latest.current; }
    catch { /* Keep the editor usable if storage is unavailable. */ }
  };
  useLayoutEffect(() => { latest.current = draft; }, [draft]);
  useEffect(() => {
    const onVisibility = () => { if (document.visibilityState === 'hidden') flush(); };
    window.addEventListener('pagehide', flush);
    document.addEventListener('visibilitychange', onVisibility);
    return () => { flush(); window.removeEventListener('pagehide', flush); document.removeEventListener('visibilitychange', onVisibility); };
  }, []);
  useEffect(() => {
    const timer = setTimeout(flush, 600);
    return () => clearTimeout(timer);
  }, [draft]);
}
