import { currentAlignment, voiceClipEnd, voiceClipStart, type VoiceClip, type VoiceSync } from './types';

export const AUTO_RATE_LIMIT = 1.15;
export const legacySync = (): VoiceSync => ({ timing_origin: 'legacy_unknown', timing_locked: true,
  text_locked: true, state: 'unverified', issues: [] });
export const automaticSync = (): VoiceSync => ({ ...legacySync(), timing_origin: 'automatic',
  timing_locked: false, text_locked: false });

export function windowIssues(clip: VoiceClip): VoiceSync['issues'] {
  if (!clip.asset_id || !clip.duration_ms) return [];
  const issues: VoiceSync['issues'] = [];
  const alignment = currentAlignment(clip);
  const start = voiceClipStart(clip) + (alignment?.speech_head_ms ?? 0);
  const end = alignment ? voiceClipStart(clip) + alignment.speech_tail_ms : voiceClipEnd(clip);
  if (start < (alignment?.source_start_ms ?? clip.start_ms) - 2) issues.push('starts_early');
  if (end > (alignment?.allowed_end_ms ?? clip.end_ms) + 2) issues.push('ends_late');
  return issues;
}

export function refreshTiming(clips: readonly VoiceClip[]): VoiceClip[] {
  const updated = clips.map(clip => {
    const issues = windowIssues(clip);
    if (!clip.asset_id || !clip.duration_ms) issues.push('missing_audio');
    const sync: VoiceSync = { ...(clip.sync ?? legacySync()),
      alignment: currentAlignment(clip),
      state: issues.length ? 'needs_review' : currentAlignment(clip) && clip.status !== 'stale' ? 'aligned' : 'unverified',
      issues: currentAlignment(clip) && clip.status !== 'stale' ? issues : [...issues, 'source_unverified'] };
    const status = clip.asset_id && ['ready', 'overflow'].includes(clip.status)
      ? (windowIssues(clip).length ? 'overflow' : 'ready') : clip.status;
    return { ...clip, status, sync } as VoiceClip & { sync: VoiceSync };
  });
  let maximumEnd = -Infinity;
  let maximumClip: typeof updated[number] | undefined;
  for (const clip of [...updated].sort((a, b) => voiceClipStart(a) - voiceClipStart(b))) {
    if (!clip.asset_id || !clip.duration_ms || clip.status === 'stale') continue;
    if (voiceClipStart(clip) < maximumEnd - 2) {
      for (const overlapping of [clip, maximumClip]) if (overlapping && !overlapping.sync.issues.includes('overlap')) {
        overlapping.sync.issues.push('overlap'); overlapping.sync.state = 'needs_review';
      }
    }
    if (voiceClipEnd(clip) > maximumEnd) { maximumEnd = voiceClipEnd(clip); maximumClip = clip; }
  }
  return updated.map((clip, i) => JSON.stringify(clip) === JSON.stringify(clips[i]) ? clips[i] : clip);
}

export const SYNC_ISSUE_LABELS = { source_unverified: 'Chưa xác minh mốc lời nói', missing_audio: 'Chưa có audio',
  starts_early: 'Bắt đầu trước vùng phụ đề', ends_late: 'Kết thúc sau vùng phụ đề', overlap: 'Audio chồng đoạn khác' };
