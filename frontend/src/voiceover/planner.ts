import type { SubtitleCueV2 } from '../subtitles/types';
import type { VoiceClip, VoiceDocument } from './types';
import { currentAlignment, voiceClipEnd, voiceClipStart } from './types';
import { AUTO_RATE_LIMIT, automaticSync, refreshTiming, windowIssues } from './timing';

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
      status: 'missing', error: null, sync: automaticSync() };
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
    if (clip.sync?.alignment && (source.length !== 1 || !source[0]
        || clip.sync.alignment.source_signature !== sourceSignature(source[0]))) {
      clip = { ...clip, sync: { ...clip.sync, alignment: null } };
      changed = true;
    }
    if (source.some(c => !c)) {
      if (clip.error === 'Phụ đề liên kết đã bị xóa hoặc tách. Cần duyệt lại đoạn giọng.') return clip;
      changed = true;
      return { ...clip, status: 'stale' as const, error: 'Phụ đề liên kết đã bị xóa hoặc tách. Cần duyệt lại đoạn giọng.' };
    }
    const rows = source as SubtitleCueV2[];
    const text = rows.map(c => c.text.trim()).join(' ');
    const locked = !clip.sync || clip.sync.timing_locked;
    const start = locked ? clip.start_ms : Math.min(...rows.map(c => c.start_ms));
    const end = locked ? clip.end_ms : Math.max(...rows.map(c => c.end_ms));
    const restored = clip.error === 'Phụ đề liên kết đã bị xóa hoặc tách. Cần duyệt lại đoạn giọng.';
    if (text === clip.source_text && start === clip.start_ms && end === clip.end_ms && !restored) return clip;
    changed = true;
    const spoken = clip.sync && !clip.sync.text_locked && clip.spoken_text === clip.source_text ? text : clip.spoken_text;
    return { ...clip, source_text: text, spoken_text: spoken, start_ms: start, end_ms: end,
      error: restored ? null : clip.error,
      offset_ms: Math.max(-start, clip.offset_ms),
      status: spoken !== clip.spoken_text && clip.asset_id ? 'stale' as const : clip.status };
  });
  return changed ? { ...doc, clips: refreshTiming(clips) } : doc;
}

export function sourceSignature(cue: SubtitleCueV2): string {
  return JSON.stringify([cue.id, cue.start_ms, cue.end_ms, cue.text, cue.source_text ?? null,
    cue.secondary_text ?? null, cue.source_language ?? null, cue.revision ?? null]);
}

export function calculateFitRate(clip: VoiceClip): number {
  if (currentAlignment(clip)) return clip.rate;
  const windowMs = clip.end_ms - voiceClipStart(clip);
  if (!clip.duration_ms || windowMs <= 0) return clip.rate;
  const needed = clip.duration_ms / windowMs;
  const rate = Math.ceil(needed * 100) / 100;
  return Math.max(clip.rate, Math.min(AUTO_RATE_LIMIT, rate));
}

export function autoFitVoiceClips(clips: readonly VoiceClip[]): VoiceClip[] {
  return refreshTiming(clips.map(clip => {
    if (!clip.duration_ms || !clip.sync || clip.sync.timing_locked || clip.sync.timing_origin !== 'automatic'
      || clip.status === 'stale' || clip.rate > AUTO_RATE_LIMIT) return clip;
    const windowMs = clip.end_ms - voiceClipStart(clip);
    if (windowMs <= 0 || voiceClipStart(clip) < clip.start_ms - 2) return clip;
    if (windowIssues(clip).includes('ends_late')) {
      const fitRate = calculateFitRate(clip);
      return {
        ...clip,
        rate: fitRate,
        sync: { ...clip.sync },
      };
    }
    return clip;
  }));
}

export function rippleShiftVoiceClips(clips: readonly VoiceClip[], gapMs = 60): VoiceClip[] {
  const sorted = [...clips].sort((a, b) => a.start_ms - b.start_ms);
  let prevEndMs = 0;
  return sorted.map((clip, index) => {
    const speechDuration = clip.duration_ms ? clip.duration_ms / clip.rate : (clip.end_ms - clip.start_ms);
    let offset_ms = clip.offset_ms;
    if (index > 0) {
      const naturalStart = clip.start_ms + offset_ms;
      if (naturalStart < prevEndMs + gapMs) {
        offset_ms = Math.max(-clip.start_ms, Math.round(prevEndMs + gapMs - clip.start_ms));
      }
    }
    const currentEnd = clip.start_ms + offset_ms + speechDuration;
    prevEndMs = currentEnd;
    return { ...clip, offset_ms };
  });
}

export function smartResolveVoiceOverlaps(clips: readonly VoiceClip[]): VoiceClip[] {
  // Source-synchronous fitting never moves the next utterance.
  return autoFitVoiceClips(clips);
}

export function resetVoiceOffsets(clips: readonly VoiceClip[]): VoiceClip[] {
  return clips.map(clip => ({ ...clip, offset_ms: 0 }));
}

export function countVoiceOverlaps(clips: readonly VoiceClip[]): { overflowCount: number; overlapCount: number } {
  let overflowCount = 0;
  let overlapCount = 0;
  const sorted = [...clips].sort((a, b) => voiceClipStart(a) - voiceClipStart(b));
  let maximumEnd = -Infinity;
  for (let i = 0; i < sorted.length; i++) {
    const clip = sorted[i];
    if (windowIssues(clip).length) {
      overflowCount++;
    }
    if (clip.asset_id && clip.duration_ms && clip.status !== 'stale') {
      if (voiceClipStart(clip) < maximumEnd - 2) overlapCount++;
      maximumEnd = Math.max(maximumEnd, voiceClipEnd(clip));
    }
  }
  return { overflowCount, overlapCount };
}
