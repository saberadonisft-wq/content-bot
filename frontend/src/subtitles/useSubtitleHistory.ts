import { useCallback, useReducer } from "react";
import type { SubtitleCueV2, SubtitleDocumentV2 } from "./types";
import { createSubtitleDocument } from "./studioConfig";
import { sortCues } from "./time";

const HISTORY_LIMIT = 60;
const GROUP_WINDOW_MS = 800;

type CueUpdater =
  | SubtitleCueV2[]
  | ((current: readonly SubtitleCueV2[]) => SubtitleCueV2[]);

export type SubtitleHistoryState = {
  past: SubtitleDocumentV2[];
  present: SubtitleDocumentV2;
  future: SubtitleDocumentV2[];
  lastGroup: string | null;
  lastCommitAt: number;
};

type HistoryAction =
  | { type: "commit"; updater: CueUpdater; group: string | null; at: number }
  | { type: "apply"; document: SubtitleDocumentV2 }
  | { type: "reset"; document: SubtitleDocumentV2 }
  | { type: "undo" }
  | { type: "redo" };

export const createSubtitleHistoryState = (
  document: SubtitleDocumentV2 = createSubtitleDocument([]),
): SubtitleHistoryState => ({
  past: [],
  present: document,
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
      present: action.document,
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
      present: { ...previous, revision: (state.present.revision ?? 0) + 1 },
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
      present: { ...next, revision: (state.present.revision ?? 0) + 1 },
      future: state.future.slice(1),
      lastGroup: null,
      lastCommitAt: 0,
    };
  }

  if (action.type === "apply") {
    return { past: [...state.past, state.present].slice(-HISTORY_LIMIT),
      present: { ...action.document, segments: sortCues(action.document.segments),
        revision: Math.max((state.present.revision ?? 0) + 1, action.document.revision ?? 0) },
      future: [], lastGroup: null, lastCommitAt: 0 };
  }

  const segments =
    typeof action.updater === "function"
      ? action.updater(state.present.segments)
      : action.updater;
  if (segments === state.present.segments) return state;
  const next = { ...state.present, ...createSubtitleDocument(segments),
    language: state.present.language, revision: (state.present.revision ?? 0) + 1 };
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

export function useSubtitleHistory(initialDocument: SubtitleDocumentV2 = createSubtitleDocument([])) {
  const [history, dispatch] = useReducer(
    subtitleHistoryReducer,
    initialDocument,
    createSubtitleHistoryState,
  );

  const commit = useCallback((updater: CueUpdater, group: string | null = null) => {
    dispatch({ type: "commit", updater, group, at: performance.now() });
  }, []);
  const reset = useCallback(
    (document: SubtitleDocumentV2 = createSubtitleDocument([])) => dispatch({ type: "reset", document }),
    [],
  );
  const undo = useCallback(() => dispatch({ type: "undo" }), []);
  const redo = useCallback(() => dispatch({ type: "redo" }), []);
  const applyDocument = useCallback((document: SubtitleDocumentV2) => dispatch({ type: "apply", document }), []);

  return {
    document: history.present,
    cues: history.present.segments,
    applyDocument,
    commit,
    reset,
    undo,
    redo,
    canUndo: history.past.length > 0,
    canRedo: history.future.length > 0,
  };
}
