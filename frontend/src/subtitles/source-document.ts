import { useReducer } from 'react';
import { createSubtitleDocument } from './studioConfig';
import { createManualCue, mergeCueWithNext, splitCueById, updateCueById } from './model';
import type { SubtitleDocumentV2, SubtitleCueV2 } from './types';

export function sourceFromDocument(document: SubtitleDocumentV2): SubtitleDocumentV2 {
  const segments = document.segments.map(cue => ({ ...cue,
    text: document.document_role === 'source' ? cue.text : cue.source_text || cue.text,
    source_text: document.document_role === 'source' ? cue.text : cue.source_text || cue.text, secondary_text: null,
  }));
  const languages = new Set(segments.map(cue => cue.source_language).filter(Boolean));
  return { ...document, document_role: 'source',
    source_revision: null, source_run_id: null, translation_models: [],
    language: languages.size === 1 ? [...languages][0]! : document.language, segments };
}

export type SourceState = {
  document: SubtitleDocumentV2 | null;
  past: SubtitleDocumentV2[];
  future: SubtitleDocumentV2[];
};
export const createSourceState = (document: SubtitleDocumentV2 | null = null): SourceState => ({ document, past: [], future: [] });
type Patch = Partial<Pick<SubtitleCueV2, 'text' | 'start_ms' | 'end_ms'>>;
type Action = { type: 'reset'; document: SubtitleDocumentV2 | null }
  | { type: 'replace'; document: SubtitleDocumentV2 }
  | { type: 'edit'; id: string; patch: Patch; durationMs?: number }
  | { type: 'delete' | 'split' | 'merge' | 'lock'; id: string }
  | { type: 'add'; durationMs: number }
  | { type: 'undo' | 'redo' };

export function sourceDocumentReducer(state: SourceState, action: Action): SourceState {
  if (action.type === 'reset') return createSourceState(action.document);
  const document = state.document;
  if (action.type === 'replace') {
    return { document: { ...sourceFromDocument(action.document), revision: (document?.revision ?? 0) + 1 },
      past: document ? [...state.past, document].slice(-60) : [], future: [] };
  }
  if (!document) return state;
  if (action.type === 'undo' || action.type === 'redo') {
    const next = action.type === 'undo' ? state.past.at(-1) : state.future[0];
    if (!next) return state;
    return { document: { ...next, revision: (document.revision ?? 0) + 1 },
      past: action.type === 'undo' ? state.past.slice(0, -1) : [...state.past, document].slice(-60),
      future: action.type === 'undo' ? [document, ...state.future].slice(0, 60) : state.future.slice(1) };
  }
  let segments = document.segments;
  if ('id' in action) {
    const cue = segments.find(c => c.id === action.id);
    if (!cue || (cue.locked && action.type !== 'lock')) return state;
    if (action.type === 'merge' && segments[segments.indexOf(cue) + 1]?.locked) return state;
  }
  switch (action.type) {
    case 'edit': segments = updateCueById(segments, action.id, action.patch, { durationMs: action.durationMs }); break;
    case 'delete': segments = segments.filter(c => c.id !== action.id); break;
    case 'split': {
      const cue = segments.find(c => c.id === action.id)!;
      segments = splitCueById(segments, action.id, Math.round((cue.start_ms + cue.end_ms) / 2)); break;
    }
    case 'merge': segments = mergeCueWithNext(segments, action.id); break;
    case 'lock': segments = segments.map(c => c.id === action.id ? { ...c, locked: !c.locked, revision: c.revision + 1 } : c); break;
    case 'add': segments = [...segments, { ...createManualCue(segments, action.durationMs), source_language: document.language }]; break;
  }
  segments = segments.map(c => c.source_text === c.text ? c : { ...c, source_text: c.text });
  return { document: { ...document, ...createSubtitleDocument(segments), language: document.language,
    run_id: document.run_id, document_role: 'source', revision: (document.revision ?? 0) + 1 },
    past: [...state.past, document].slice(-60), future: [] };
}

export function useSourceDocument(initial: SubtitleDocumentV2 | null) {
  const [state, dispatch] = useReducer(sourceDocumentReducer, initial, createSourceState);
  return { ...state, dispatch };
}
