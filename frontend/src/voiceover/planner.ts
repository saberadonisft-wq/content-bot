import type { SubtitleCueV2 } from '../subtitles/types';
import type { VoiceClip, VoiceDocument } from './types';
import { voiceClipEnd, voiceClipStart } from './types';

export function voiceClipsInRange(clips: readonly VoiceClip[], startMs: number, endMs: number): string[] {
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs) || startMs < 0 || endMs <= startMs) return [];
  return clips.filter(clip => voiceClipStart(clip) < endMs && voiceClipEnd(clip) > startMs).map(clip => clip.id);
}

export function planVoiceClips(cues: readonly SubtitleCueV2[]): VoiceClip[] {
  const groups: SubtitleCueV2[][] = [];
  for (const cue of [...cues].sort((a, b) => a.start_ms - b.start_ms)) {
    if (!cue.text.trim()) continue;
    const current = groups.at(-1);
    const last = current?.at(-1);
    if (current && last && cue.start_ms >= last.end_ms && cue.start_ms - last.end_ms <= 350
        && cue.end_ms - current[0].start_ms <= 20000
        && current.map(c => c.text).join(' ').length + cue.text.length < 500
        && !/[.!?…。！？][”"']?$/.test(last.text.trim())) current.push(cue);
    else groups.push([cue]);
  }
  return groups.map(group => {
    const text = group.map(c => c.text.trim()).join(' ');
    return { id: `v-${crypto.randomUUID()}`, source_cue_ids: group.map(c => c.id), source_text: text,
      spoken_text: text, start_ms: group[0].start_ms, end_ms: group.at(-1)!.end_ms,
      offset_ms: 0, rate: 1.08, gain: 1, asset_id: null, generation_hash: null, duration_ms: 0,
      status: 'missing', error: null };
  });
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
