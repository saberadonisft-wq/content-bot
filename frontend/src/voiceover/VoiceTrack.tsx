import { memo, useEffect, useState } from 'react';
import { voiceRequest } from './api';
import { VOICE_STATUS_LABELS, voiceClipEnd, voiceClipStart, type VoiceClip, type VoiceJob } from './types';

function voiceClipLabel(clip: VoiceClip, job?: VoiceJob | null): string {
  if (job && ['queued', 'running'].includes(job.state)
      && (!job.clip_ids || job.clip_ids.includes(clip.id)) && !job.completed_clip_ids?.includes(clip.id)) {
    if (job.failed.some(failure => failure.clip_id === clip.id)) return 'Lỗi';
    if (job.current_clip_id === clip.id) return 'Đang tạo';
    if (clip.status !== 'ready') return 'Đang chờ';
  }
  if (clip.sync?.issues.includes('overlap')) return 'Chồng audio';
  if (clip.status === 'ready' && clip.sync?.state !== 'aligned') return 'Có giọng · chưa xác minh';
  return VOICE_STATUS_LABELS[clip.status];
}

const VoiceBlock = memo(function VoiceBlock({ clip, label, pixelsPerSecond, selected, onSelect, onMove }: {
  label: string;
  clip: VoiceClip; pixelsPerSecond: number; selected: boolean;
  onSelect: (id: string) => void; onMove: (id: string, offset: number) => void;
}) {
  const [waveform, setWaveform] = useState<{ asset: string; peaks: number[] } | null>(null);
  const peaks = waveform?.asset === clip.asset_id ? waveform.peaks : [];
  useEffect(() => {
    const controller = new AbortController();
    if (clip.asset_id) void voiceRequest<{ peaks: number[][] }>(`/assets/${clip.asset_id}/peaks`, { signal: controller.signal })
      .then(meta => { if (!controller.signal.aborted) setWaveform({ asset: clip.asset_id!, peaks: meta.peaks[0] ?? [] }); }).catch(() => {});
    return () => controller.abort();
  }, [clip.asset_id]);
  const width = Math.max(18, (voiceClipEnd(clip) - voiceClipStart(clip)) / 1000 * pixelsPerSecond);
  return <button type="button" className={`voice-clip is-${clip.status} ${selected ? 'is-selected' : ''}`}
    style={{ left: voiceClipStart(clip) / 1000 * pixelsPerSecond, width }}
    title={`${clip.spoken_text} · ${label}`} aria-pressed={selected}
    aria-label={`${clip.spoken_text}, ${label}`}
    onClick={() => onSelect(clip.id)}
    onKeyDown={event => { if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault(); event.stopPropagation(); onMove(clip.id, Math.max(-clip.start_ms, clip.offset_ms + (event.key === 'ArrowRight' ? 1 : -1) * (event.shiftKey ? 1000 : 10)));
    } }}
    onPointerDown={event => {
      if (event.button !== 0) return;
      const element = event.currentTarget; const origin = event.clientX;
      element.setPointerCapture(event.pointerId); onSelect(clip.id);
      const move = (e: PointerEvent) => { element.style.transform = `translateX(${Math.max(-voiceClipStart(clip) / 1000 * pixelsPerSecond, e.clientX - origin)}px)`; };
      const cleanup = () => { element.removeEventListener('pointermove', move); element.removeEventListener('pointerup', up); element.removeEventListener('pointercancel', cancel); element.style.transform = ''; };
      const up = (e: PointerEvent) => { cleanup(); if (Math.abs(e.clientX - origin) > 3) onMove(clip.id, Math.max(-clip.start_ms, clip.offset_ms + Math.round((e.clientX - origin) / pixelsPerSecond * 1000))); };
      const cancel = () => cleanup();
      element.addEventListener('pointermove', move); element.addEventListener('pointerup', up); element.addEventListener('pointercancel', cancel);
    }}>
    <span>{clip.spoken_text}</span>
    <svg aria-hidden="true" viewBox="0 0 250 24" preserveAspectRatio="none">
      <path d={peaks.map((peak, i) => `M${i / Math.max(1, peaks.length - 1) * 250},${12 - peak * 11}v${Math.max(0.5, peak * 22)}`).join(' ')} />
    </svg><small>{label}</small>
  </button>;
});

export function VoiceTrack({ clips, job, selectedId, pixelsPerSecond, startMs, endMs, onSelect, onMove }: {
  job?: VoiceJob | null;
  clips: readonly VoiceClip[]; selectedId: string | null; pixelsPerSecond: number; startMs: number; endMs: number;
  onSelect: (id: string) => void; onMove: (id: string, offset: number) => void;
}) {
  return <div className="voice-track-row" aria-label="Hàng giọng đọc AI">
    {clips.filter(c => voiceClipStart(c) <= endMs && voiceClipEnd(c) >= startMs).map(c => <VoiceBlock
      key={c.id} clip={c} label={voiceClipLabel(c, job)} pixelsPerSecond={pixelsPerSecond} selected={c.id === selectedId} onSelect={onSelect} onMove={onMove} />)}
    {!clips.length && <span className="subtitle-track-empty">Giọng đọc AI · tạo đoạn từ phụ đề để bắt đầu</span>}
  </div>;
}
