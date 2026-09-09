import { useEffect } from 'react';
import { voiceFetch } from './api';
import { voiceDuckGain } from './duck';
import { voiceClipEnd, voiceClipStart, type VoiceDocument } from './types';

/** Keep only current/next audio in memory. Video time is the single transport clock. */
export function useVoicePlayback(video: HTMLVideoElement | null, doc: VoiceDocument | null,
  enabled: boolean, sourceVolume: number, onError?: (message: string) => void) {
  useEffect(() => {
    if (!video || !doc || !enabled || !doc.mix.enabled) return;
    const audio = new Audio();
    audio.preservesPitch = true;
    const context = new AudioContext();
    const gain = context.createGain();
    context.createMediaElementSource(audio).connect(gain).connect(context.destination);
    const clips = doc.clips.filter(c => c.asset_id && c.status !== 'stale').sort((a, b) => voiceClipStart(a) - voiceClipStart(b));
    const blobs = new Map<string, string>();
    const pending = new Map<string, AbortController>();
    let selected: string | null = null;
    let ended = false;
    let frame = 0;
    let buffered = false;
    let errorReported = false;
    const report = (e: unknown) => { if (!errorReported && !ended) { errorReported = true; onError?.(`Không phát được giọng: ${String(e)}`); } };
    const load = async (id: string) => {
      if (blobs.has(id) || pending.has(id)) return;
      const controller = new AbortController(); pending.set(id, controller);
      try {
        const response = await voiceFetch(`/assets/${id}`, { signal: controller.signal });
        const blob = await response.blob();
        if (!ended && !controller.signal.aborted) blobs.set(id, URL.createObjectURL(blob));
      } catch (e) { if (!controller.signal.aborted) report(e); }
      finally { if (pending.get(id) === controller) pending.delete(id); }
    };
    const tick = () => {
      if (ended) return;
      const ms = video.currentTime * 1000;
      let lo = 0, hi = clips.length;
      while (lo < hi) { const mid = (lo + hi) >>> 1; if (voiceClipStart(clips[mid]) <= ms) lo = mid + 1; else hi = mid; }
      const candidate = clips[lo - 1];
      const active = candidate && ms < voiceClipEnd(candidate) ? candidate : null;
      const next = clips[lo];
      const keep = new Set([active?.asset_id, next?.asset_id].filter((x): x is string => Boolean(x)));
      for (const id of keep) void load(id);
      for (const [id, url] of blobs) if (!keep.has(id)) { URL.revokeObjectURL(url); blobs.delete(id); }
      for (const [id, controller] of pending) if (!keep.has(id)) controller.abort();
      const url = active?.asset_id ? blobs.get(active.asset_id) : null;
      const playing = !video.paused && !video.ended && !video.seeking && !buffered && video.readyState >= 2;
      audio.muted = video.muted;
      if (active && url) {
        const desired = (ms - voiceClipStart(active)) / 1000 * active.rate;
        if (selected !== active.id) {
          audio.pause(); audio.src = url; selected = active.id;
          audio.currentTime = Math.max(0, desired);
        } else if (Math.abs(audio.currentTime - desired) > 0.06) audio.currentTime = Math.max(0, desired);
        audio.playbackRate = Math.max(0.25, Math.min(4, active.rate * video.playbackRate));
        gain.gain.value = doc.mix.muted ? 0 : doc.mix.gain * active.gain;
        if (playing && audio.paused && context.state === 'running') void audio.play().catch(report);
        if (!playing) audio.pause();
      } else { audio.pause(); selected = null; }
      const duck = doc.mix.mode === 'duck' && !doc.mix.muted && doc.mix.gain > 0 ? voiceDuckGain(clips, lo, ms, video.playbackRate) : 1;
      const original = doc.mix.mode === 'voice' ? 0 : Math.min(1, sourceVolume * doc.mix.original_gain * duck);
      video.volume = original;
      frame = requestAnimationFrame(tick);
    };
    const pause = () => audio.pause();
    const waiting = () => { buffered = true; audio.pause(); };
    const playing = () => { buffered = false; void context.resume().catch(report); };
    video.addEventListener('pause', pause); video.addEventListener('seeking', pause);
    video.addEventListener('waiting', waiting); video.addEventListener('playing', playing);
    video.addEventListener('play', playing);
    if (!video.paused) playing();
    frame = requestAnimationFrame(tick);
    return () => {
      ended = true; cancelAnimationFrame(frame); audio.pause(); audio.removeAttribute('src'); audio.load();
      video.removeEventListener('pause', pause); video.removeEventListener('seeking', pause);
      video.removeEventListener('waiting', waiting); video.removeEventListener('playing', playing); video.removeEventListener('play', playing);
      pending.forEach(c => c.abort()); blobs.forEach(url => URL.revokeObjectURL(url));
      void context.close(); video.volume = Math.min(1, sourceVolume);
    };
  }, [video, doc, enabled, sourceVolume, onError]);
}
