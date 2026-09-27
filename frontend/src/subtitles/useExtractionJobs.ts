import { useCallback, useLayoutEffect, useRef, useState } from 'react';
import { api, type SubtitleJob, type SubtitleExtractionResult } from '../api';
import { useJobPolling } from './useJobPolling';
import type { SubtitleDocumentV2 } from './types';

export type ExtractionKind = 'ocr' | 'asr' | 'translation';
export type PendingExtraction = { id: string; videoId: string; snapshot: string; kind: ExtractionKind };
export async function snapshotDigest(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value));
  return [...new Uint8Array(digest)].map(byte => byte.toString(16).padStart(2, '0')).join('');
}

// Draft migration fills nullable/default fields. Compare equivalent documents consistently on reload.
export function extractionSnapshot(videoId: string | null, document: SubtitleDocumentV2,
  source: SubtitleDocumentV2 | null, configuration: string): string {
  const doc = (value: SubtitleDocumentV2 | null) => value && ({ ...value, revision: value.revision ?? 0,
    translation_models: value.translation_models ?? [],
    segments: value.segments.map(cue => ({ ...cue, locked: cue.locked ?? false,
      content_source: cue.content_source ?? 'unknown' })) });
  const stable = (value: unknown): unknown => {
    if (Array.isArray(value)) return value.map(stable);
    if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value)
      .filter(([, v]) => v !== null && v !== undefined).sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => [k, stable(v)]));
    return value;
  };
  return JSON.stringify(stable({ videoId, document: doc(document), source: doc(source), configuration }));
}

/** Tracks the whole submit + poll lifetime, including edits while a response is in flight. */
export function useExtractionJobs(options: {
  videoId: string | null; document: SubtitleDocumentV2; source: SubtitleDocumentV2 | null;
  configuration: string; initial?: PendingExtraction | null;
  onSource: (document: SubtitleDocumentV2, result: SubtitleExtractionResult) => void;
  onTranslation: (document: SubtitleDocumentV2) => void;
  onError: (message: string | null) => void; onNotice: (message: string) => void;
}) {
  const [pending, setPending] = useState<PendingExtraction | null>(() => options.initial?.videoId === options.videoId ? options.initial : null);
  const [job, setJob] = useState<SubtitleJob | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [versionReload, setVersionReload] = useState(0);
  const live = useRef(options);
  const lifetime = useRef(0);
  const controller = useRef<AbortController | null>(null);
  const active = useRef<PendingExtraction | null>(pending);
  const lastVideo = useRef(options.videoId);
  useLayoutEffect(() => { live.current = options; });
  const snapshot = useCallback(() => extractionSnapshot(live.current.videoId,
    live.current.document, live.current.source, live.current.configuration), []);
  const invalidate = useCallback(() => {
    lifetime.current++;
    controller.current?.abort();
    controller.current = null;
  }, []);
  const reset = useCallback(() => {
    invalidate();
    active.current = null;
    setPending(null); setJob(null); setSubmitting(false);
  }, [invalidate]);
  useLayoutEffect(() => {
    // Reset synchronously at a video boundary, preserving a matching persisted job on mount.
    if (lastVideo.current !== options.videoId) { reset(); lastVideo.current = options.videoId; }
    return invalidate;
  }, [invalidate, options.videoId, reset]);

  const start = useCallback(async (kind: ExtractionKind, request: (signal: AbortSignal) => Promise<SubtitleJob>) => {
    if (!live.current.videoId || controller.current || active.current) return;
    const capturedVideo = live.current.videoId;
    const capturedSnapshot = snapshot();
    const token = lifetime.current;
    const abort = new AbortController(); controller.current = abort;
    setSubmitting(true); live.current.onError(null);
    try {
      const signature = await snapshotDigest(capturedSnapshot);
      if (abort.signal.aborted || token !== lifetime.current) return;
      const result = await request(abort.signal);
      if (abort.signal.aborted || token !== lifetime.current || capturedVideo !== live.current.videoId) return;
      const next = { id: result.id, kind, videoId: capturedVideo, snapshot: signature };
      active.current = next; setPending(next); setJob(result);
    } catch (error) {
      if (!abort.signal.aborted && token === lifetime.current) live.current.onError(String(error));
    } finally {
      if (controller.current === abort) { controller.current = null; setSubmitting(false); }
    }
  }, [snapshot]);

  const accept = useCallback(async (next: SubtitleJob) => {
    const bound = active.current;
    if (!bound || next.id !== bound.id || bound.videoId !== live.current.videoId) return;
    setJob(next);
    if (!['succeeded', 'failed', 'canceled'].includes(next.state)) return;
    const token = lifetime.current;
    const atResponse = snapshot();
    const signature = await snapshotDigest(atResponse);
    if (token !== lifetime.current || active.current !== bound || bound.videoId !== live.current.videoId) return;
    active.current = null; setPending(null);
    setVersionReload(n => n + 1);
    if (next.state === 'succeeded') {
      const result = next.result as SubtitleExtractionResult | null | undefined;
      if (signature !== bound.snapshot || snapshot() !== atResponse) {
        live.current.onNotice('Phụ đề hoặc cấu hình đã thay đổi. Kết quả được giữ trong Phiên bản phụ đề; bản đang chỉnh được giữ nguyên.');
      } else if ((bound.kind === 'translation' ? live.current.document : live.current.source)?.segments.some(cue => cue.locked)) {
        live.current.onNotice('Tài liệu có câu đã khóa. Kết quả được giữ trong Phiên bản phụ đề để đối chiếu trước khi áp dụng.');
      } else if (result?.document) {
        if (bound.kind === 'translation') live.current.onTranslation(result.document);
        else live.current.onSource(result.document, result);
      }
    } else if (next.state === 'failed') live.current.onError(next.error || 'Tác vụ thất bại.');
  }, [snapshot]);
  useJobPolling({ jobId: pending?.videoId === options.videoId ? pending.id : null,
    fetchJob: api.subtitleJob, interval: 800, retryDelay: 1500,
    onJob: next => { void accept(next).catch(error => live.current.onError(String(error))); },
    onError: () => live.current.onError('Mất kết nối khi đọc tiến độ; đang thử lại.') });
  const cancel = useCallback(async () => {
    const bound = active.current;
    if (!bound) { reset(); return; }
    try {
      const result = await api.cancelSubtitleJob(bound.id);
      if (active.current === bound) setJob(result);
    } catch (error) { if (active.current === bound) live.current.onError(String(error)); }
  }, [reset]);
  return { job, pending, running: submitting || Boolean(pending), submitting, start, cancel, reset, versionReload };
}
