import { useEffect, useEffectEvent, useRef } from 'react';
import { VoicePlaybackEngine } from './playbackEngine';
import type { VoiceDocument } from './types';

export function useVoicePlayback(video: HTMLVideoElement | null, doc: VoiceDocument | null,
  enabled: boolean, sourceVolume: number, onError?: (message: string) => void) {
  const engine = useRef<VoicePlaybackEngine | null>(null);
  const report = useEffectEvent((message: string) => onError?.(message));
  const projectId = doc?.project_id;
  useEffect(() => {
    if (!video || !projectId || !enabled) return;
    const playback = new VoicePlaybackEngine(video, report);
    engine.current = playback;
    return () => {
      playback.dispose();
      if (engine.current === playback) engine.current = null;
    };
  }, [video, projectId, enabled]);
  useEffect(() => {
    if (doc) engine.current?.update(doc, sourceVolume);
  }, [video, doc, enabled, sourceVolume]);
}
