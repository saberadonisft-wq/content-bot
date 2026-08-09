import { useSyncExternalStore } from "react";
import type { PlaybackClockStore } from "./playback-store";

export const usePlaybackSnapshot = (store: PlaybackClockStore) =>
  useSyncExternalStore(store.subscribe, store.getSnapshot, store.getSnapshot);

export const usePlaybackDuration = (store: PlaybackClockStore) =>
  useSyncExternalStore(
    store.subscribe,
    () => store.getSnapshot().durationMs,
    () => store.getSnapshot().durationMs,
  );
