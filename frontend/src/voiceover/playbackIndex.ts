import { voiceClipEnd, voiceClipStart, type VoiceClip } from './types';

/** Sorted playback projection, rebuilt only when the clip collection changes. */
export class PlaybackIndex {
  readonly clips: readonly VoiceClip[];
  private readonly starts: number[];
  private readonly maximumEnds: number[];

  constructor(clips: readonly VoiceClip[]) {
    this.clips = clips.filter(clip => clip.asset_id && clip.status !== 'stale')
      .sort((a, b) => voiceClipStart(a) - voiceClipStart(b));
    this.starts = this.clips.map(voiceClipStart);
    let maximum = -Infinity;
    this.maximumEnds = this.clips.map(clip => (maximum = Math.max(maximum, voiceClipEnd(clip))));
  }

  nextIndex(ms: number): number {
    let lo = 0, hi = this.starts.length;
    while (lo < hi) {
      const mid = (lo + hi) >>> 1;
      if (this.starts[mid] <= ms) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  active(ms: number, nextIndex = this.nextIndex(ms)): VoiceClip | null {
    // Preserve the existing preview policy: latest started clip wins; an earlier
    // overlapping clip is not resumed when that latest clip ends.
    const candidate = this.clips[nextIndex - 1];
    return candidate && ms < voiceClipEnd(candidate) ? candidate : null;
  }

  windowAssets(ms: number, nextIndex = this.nextIndex(ms), maximum = 24): string[] {
    const ids = new Set<string>();
    const add = (clip: VoiceClip | null | undefined) => {
      if (clip?.asset_id && ids.size < maximum) ids.add(clip.asset_id);
    };
    add(this.active(ms, nextIndex));
    add(this.clips[nextIndex]);
    // Prioritize upcoming audio; historic audio is useful for short seeks only.
    for (let i = nextIndex + 1; i < this.clips.length && ids.size < maximum; i++) {
      if (this.starts[i] > ms + 20_000) break;
      add(this.clips[i]);
    }
    for (let i = nextIndex - 1; i >= 0 && ids.size < maximum; i--) {
      if (this.maximumEnds[i] < ms - 5_000) break;
      if (voiceClipEnd(this.clips[i]) >= ms - 5_000) add(this.clips[i]);
    }
    return [...ids];
  }
}
