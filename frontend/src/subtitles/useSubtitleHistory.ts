import { useCallback, useReducer } from "react";
import type { SubtitleCueV2 } from "./types";

const HISTORY_LIMIT = 60;
const GROUP_WINDOW_MS = 800;

type CueUpdater =
  | SubtitleCueV2[]
  | ((current: readonly SubtitleCueV2[]) => SubtitleCueV2[]);

export type SubtitleHistoryState = {
  past: SubtitleCueV2[][];
  present: SubtitleCueV2[];
  future: SubtitleCueV2[][];
  lastGroup: string | null;
  lastCommitAt: number;
};

type HistoryAction =
  | { type: "commit"; updater: CueUpdater; group: string | null; at: number }
  | { type: "reset"; cues: SubtitleCueV2[] }
  | { type: "undo" }
  | { type: "redo" };

export const createSubtitleHistoryState = (
  cues: SubtitleCueV2[] = [],
): SubtitleHistoryState => ({
  past: [],
  present: cues,
  future: [],
  lastGroup: null,
  lastCommitAt: 0,
});

export const subtitleHistoryReducer = (
  state: SubtitleHistoryState,
  action: HistoryAction,
): SubtitleHistoryState => {
  if (action.type === "reset") {
    return {
      past: [],
      present: action.cues,
      future: [],
      lastGroup: null,
      lastCommitAt: 0,
    };
  }
  if (action.type === "undo") {
    const previous = state.past.at(-1);
    if (!previous) return state;
    return {
      past: state.past.slice(0, -1),
      present: previous,
      future: [state.present, ...state.future].slice(0, HISTORY_LIMIT),
      lastGroup: null,
      lastCommitAt: 0,
    };
  }
  if (action.type === "redo") {
    const next = state.future[0];
    if (!next) return state;
    return {
      past: [...state.past, state.present].slice(-HISTORY_LIMIT),
      present: next,
      future: state.future.slice(1),
      lastGroup: null,
      lastCommitAt: 0,
    };
  }

  const next =
    typeof action.updater === "function"
      ? action.updater(state.present)
      : action.updater;
  if (next === state.present) return state;
  const grouped = Boolean(
    action.group &&
      state.lastGroup === action.group &&
      action.at - state.lastCommitAt <= GROUP_WINDOW_MS,
  );
  return {
    past: grouped
      ? state.past
      : [...state.past, state.present].slice(-HISTORY_LIMIT),
    present: next,
    future: [],
    lastGroup: action.group,
    lastCommitAt: action.at,
  };
};

export function useSubtitleHistory(initialCues: SubtitleCueV2[] = []) {
  const [history, dispatch] = useReducer(
    subtitleHistoryReducer,
    initialCues,
    createSubtitleHistoryState,
  );

  const commit = useCallback((updater: CueUpdater, group: string | null = null) => {
    dispatch({ type: "commit", updater, group, at: performance.now() });
  }, []);
  const reset = useCallback(
    (cues: SubtitleCueV2[] = []) => dispatch({ type: "reset", cues }),
    [],
  );
  const undo = useCallback(() => dispatch({ type: "undo" }), []);
  const redo = useCallback(() => dispatch({ type: "redo" }), []);

  return {
    cues: history.present,
    commit,
    reset,
    undo,
    redo,
    canUndo: history.past.length > 0,
    canRedo: history.future.length > 0,
  };
}
