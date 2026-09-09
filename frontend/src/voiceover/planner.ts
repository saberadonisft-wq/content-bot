import type { SubtitleCueV2 } from '../subtitles/types';
import type { VoiceClip, VoiceDocument } from './types';
import { voiceClipEnd, voiceClipStart } from './types';

export function voiceClipsInRange(clips: readonly VoiceClip[], startMs: number, endMs: number): string[] {
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs < 0 || endMs <= startMs) return [];
  return clips.filter(clip => voiceClipStart(clip) < endMs && voiceClipEnd(clip) > startMs).map(clip => clip.id);
}

export function planVoiceClips(cues: readonly SubtitleCueV2[]): VoiceClip[] {
  // A single TTS asset has no internal cue timestamps. Joining adjacent cues
  // makes later lines play before their subtitles appear.
  return [...cues].filter(cue => cue.text.trim()).sort((a, b) => a.start_ms - b.start_ms).map(cue => {
    const text = cue.text.trim();
    return { id: `v-${crypto.randomUUID()}`, source_cue_ids: [cue.id], source_text: text,
      spoken_text: text, start_ms: cue.start_ms, end_ms: cue.end_ms,
      offset_ms: 0, rate: 1.08, gain: 1, asset_id: null, generation_hash: null, duration_ms: 0,
      status: 'missing', error: null };
  });
}

export function splitGroupedVoiceClips(doc: VoiceDocument, cues: readonly SubtitleCueV2[]): {
  document: VoiceDocument; skipped: number;
} {
  const index = new Map(cues.map(cue => [cue.id, cue]));
  const references = new Map<string, number>();
  for (const clip of doc.clips) for (const id of clip.source_cue_ids) references.set(id, (references.get(id) ?? 0) + 1);
  let changed = false;
  let skipped = 0;
  const clips = doc.clips.flatMap(clip => {
    if (clip.source_cue_ids.length <= 1) return [clip];
    const source = clip.source_cue_ids.map(id => index.get(id));
    // Custom narration cannot be divided safely without knowing which words
    // belong to each cue. Leave it available for the user to review.
    if (clip.spoken_text !== clip.source_text || source.some(cue => !cue?.text.trim())
        || clip.source_cue_ids.some(id => references.get(id) !== 1)) {
      skipped++;
      return [clip];
    }
    changed = true;
    return planVoiceClips(source as SubtitleCueV2[]).map(next => ({ ...next, rate: clip.rate, gain: clip.gain }));
  });
  return { document: changed ? { ...doc, clips: clips.sort((a, b) => a.start_ms - b.start_ms) } : doc, skipped };
}

export function syncVoiceCues(doc: VoiceDocument, cues: readonly SubtitleCueV2[]): VoiceDocument {
  const index = new Map(cues.map(c => [c.id, c]));
  let changed = false;
  const clips = doc.clips.map(clip => {
    if (!clip.source_cue_ids.length) return clip;
    const source = clip.source_cue_ids.map(id => index.get(id));
    if (source.some(c => !c)) {
      if (clip.error === 'Phụ đề liên kết đã bị xóa hoặc tách. Cần duyệt lại đoạn giọng.') return clip;
      changed = true;
      return { ...clip, status: 'stale' as const, error: 'Phụ đề liên kết đã bị xóa hoặc tách. Cần duyệt lại đoạn giọng.' };
    }
    const rows = source as SubtitleCueV2[];
    const text = rows.map(c => c.text.trim()).join(' ');
    const start = Math.min(...rows.map(c => c.start_ms));
    const end = Math.max(...rows.map(c => c.end_ms));
    const restored = clip.error === 'Phụ đề liên kết đã bị xóa hoặc tách. Cần duyệt lại đoạn giọng.';
    if (text === clip.source_text && start === clip.start_ms && end === clip.end_ms && !restored) return clip;
    changed = true;
    const spoken = clip.spoken_text === clip.source_text ? text : clip.spoken_text;
    return { ...clip, source_text: text, spoken_text: spoken, start_ms: start, end_ms: end,
      error: restored ? null : clip.error,
      offset_ms: Math.max(-start, clip.offset_ms),
      status: spoken !== clip.spoken_text && clip.asset_id ? 'stale' as const : clip.status };
  });
  return changed ? { ...doc, clips } : doc;
}
