import { voiceClipEnd, voiceClipStart, type VoiceClip } from './types';

/** Same 20 ms attack / 250 ms release and 0.25 floor as the export control track. */
export function voiceDuckGain(clips: readonly VoiceClip[], nextIndex: number, ms: number, playbackRate = 1): number {
  const attack = 20 * playbackRate;
  const release = 250 * playbackRate;
  let factor = 1;
  for (let i = nextIndex - 1; i >= 0; i--) {
    const clip = clips[i];
    const end = voiceClipEnd(clip);
    if (end + release <= ms) break;
    if (!clip.asset_id || clip.gain <= 0) continue;
    const depth = Math.max(0, Math.min(1, (ms - voiceClipStart(clip)) / attack, (end + release - ms) / release));
    factor = Math.min(factor, 1 - 0.75 * depth);
  }
  return factor;
}
