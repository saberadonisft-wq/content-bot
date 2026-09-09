import { useCallback, useEffect, useRef, useState } from 'react';
import type { SubtitleCueV2 } from '../subtitles/types';
import { VoiceApiError, voiceRequest } from './api';
import { DEFAULT_PROFILE, type VoiceDocument, type VoiceJob, type VoiceStatus } from './types';
import { planVoiceClips, syncVoiceCues } from './planner';
import { mergeVoiceDocument } from './merge';

export function useVoiceover(project: string | null, fingerprint: string | undefined, cues: readonly SubtitleCueV2[]) {
  const [document, setState] = useState<VoiceDocument | null>(null);
  const [status, setStatus] = useState<VoiceStatus | null>(null);
  const [job, setJob] = useState<VoiceJob | null>(null);
  const [error, setError] = useState('');
  const [conflict, setConflict] = useState<{ base: VoiceDocument; local: VoiceDocument; remote: VoiceDocument } | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [preferredDevice, setDevice] = useState<'cpu' | 'cuda'>('cpu');
  const device = status?.devices.includes(preferredDevice) ? preferredDevice
    : status?.devices.includes('cpu') ? 'cpu' : status?.devices.includes('cuda') ? 'cuda' : preferredDevice;
  const [busy, setBusy] = useState(false);
  const [loadedProject, setLoadedProject] = useState<string | null>(null);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [saveState, setSaveState] = useState<'saved' | 'dirty' | 'saving' | 'error'>('saved');
  const current = useRef(document);
  const baseline = useRef<VoiceDocument | null>(null);
  const dirty = useRef(false);
  const saving = useRef(false);
  const projectRef = useRef(project);
  const undo = useRef<VoiceDocument[]>([]);
  const redo = useRef<VoiceDocument[]>([]);
  const replace = useCallback((doc: VoiceDocument | null) => { current.current = doc; setState(doc); }, []);
  const acceptServerDocument = useCallback((doc: VoiceDocument | null) => {
    if (!doc || doc.project_id !== projectRef.current) return;
    if (dirty.current && current.current) {
      // A package upload can finish after more typing. Preserve those edits,
      // and compare against the server version observed before the upload.
      let merged: VoiceDocument;
      try { merged = mergeVoiceDocument(baseline.current, current.current, doc); }
      catch (e) {
        if (baseline.current) setConflict({ base: baseline.current, local: current.current, remote: doc });
        throw e;
      }
      baseline.current = doc; replace(merged); setSaveState('dirty');
    } else {
      baseline.current = doc; dirty.current = false; setSaveState('saved'); replace(doc);
    }
  }, [replace]);
  const edit = useCallback((fn: (doc: VoiceDocument) => VoiceDocument) => {
    if (!current.current) return;
    undo.current = [...undo.current.slice(-29), current.current]; redo.current = [];
    dirty.current = true; setSaveState('dirty'); replace(fn(current.current));
  }, [replace]);
  const refreshStatus = useCallback(async () => {
    try { setStatus(await voiceRequest<VoiceStatus>('/status')); }
    catch (e) { setError(String(e)); }
  }, []);
  useEffect(() => {
    const controller = new AbortController();
    void voiceRequest<VoiceStatus>('/status', { signal: controller.signal })
      .then(setStatus).catch(e => { if (!controller.signal.aborted) setError(String(e)); });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    projectRef.current = project; dirty.current = false; baseline.current = null; undo.current = []; redo.current = [];
    const controller = new AbortController();
    void (async () => {
      setLoadedProject(null); setConflict(null); replace(null); setJob(null);
      if (!project) { replace(null); setJob(null); return; }
      try {
        const [doc, jobs] = await Promise.all([
          voiceRequest<VoiceDocument>(`/projects/${project}`, { signal: controller.signal }).catch(e => {
            if (e instanceof VoiceApiError && e.status === 404) return null;
            throw e;
          }), voiceRequest<VoiceJob[]>(`/projects/${project}/jobs`, { signal: controller.signal }),
        ]);
        if (!controller.signal.aborted) { baseline.current = doc; replace(doc); setSaveState('saved'); setJob(jobs[0] ?? null); setSelectedId(null); setLoadedProject(project); }
      } catch (e) { if (!controller.signal.aborted) setError(String(e)); }
    })();
    return () => controller.abort();
  }, [project, replace, loadAttempt]);
  useEffect(() => {
    if (!current.current || current.current.project_id !== project) return;
    const next = syncVoiceCues(current.current, cues);
    if (next !== current.current) {
      dirty.current = true;
      // Synchronize source edits without creating an undo entry for every subtitle keystroke.
      const source = current.current;
      queueMicrotask(() => {
        if (projectRef.current === project && current.current === source) { setSaveState('dirty'); replace(next); }
      });
    }
  }, [cues, project, replace]);

  const save = useCallback(async () => {
    if (saving.current) throw new Error('Đang lưu lời đọc, vui lòng thử lại sau giây lát.');
    saving.current = true;
    setSaveState('saving');
    try {
    const doc = current.current;
    if (!doc || doc.project_id !== projectRef.current) throw new Error('Chưa có dự án giọng đọc.');
    const base = baseline.current;
    const latest = await voiceRequest<VoiceDocument>(`/projects/${doc.project_id}`).catch(e => {
      if (e instanceof VoiceApiError && e.status === 404) return null;
      throw e;
    });
    const merge = (remote: VoiceDocument | null) => {
      try { return mergeVoiceDocument(base, doc, remote); }
      catch (e) {
        if (base && remote && projectRef.current === doc.project_id) setConflict({ base, local: doc, remote });
        throw e;
      }
    };
    let payload = merge(latest);
    let saved: VoiceDocument;
    try {
      saved = await voiceRequest<VoiceDocument>(`/projects/${doc.project_id}`, { method: 'PUT', body: JSON.stringify(payload) });
    } catch (e) {
      if (!(e instanceof VoiceApiError) || e.status !== 409) throw e;
      // Worker may attach audio between GET and PUT. Retry once with its new revision.
      const refreshed = await voiceRequest<VoiceDocument>(`/projects/${doc.project_id}`);
      payload = merge(refreshed);
      saved = await voiceRequest<VoiceDocument>(`/projects/${doc.project_id}`, { method: 'PUT', body: JSON.stringify(payload) });
    }
    if (projectRef.current === doc.project_id && current.current === doc) {
      baseline.current = saved; dirty.current = false; setSaveState('saved'); replace(saved);
    } else if (projectRef.current === doc.project_id && current.current) {
      let rebased: VoiceDocument;
      try { rebased = mergeVoiceDocument(doc, current.current, saved); }
      catch (e) { setConflict({ base: doc, local: current.current, remote: saved }); throw e; }
      baseline.current = saved; replace(rebased); setSaveState('dirty');
    }
    return saved;
    } catch (e) { setSaveState('error'); throw e; }
    finally { saving.current = false; }
  }, [replace]);

  useEffect(() => {
    if (!document) return;
    const timer = setInterval(() => {
      if (dirty.current && !saving.current && !conflict) void save().catch(e => setError(String(e)));
    }, 1500);
    return () => clearInterval(timer);
  }, [document, save, conflict]);

  const resolveConflict = (choice: 'local' | 'remote') => {
    if (!conflict || !current.current || conflict.remote.project_id !== projectRef.current) return;
    const merged = mergeVoiceDocument(conflict.base, current.current, conflict.remote, choice);
    baseline.current = conflict.remote;
    dirty.current = true; replace(merged); setSaveState('dirty'); setConflict(null); setError('');
  };

  useEffect(() => {
    const warnUnsaved = (event: BeforeUnloadEvent) => {
      if (!dirty.current && !saving.current) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', warnUnsaved);
    return () => window.removeEventListener('beforeunload', warnUnsaved);
  }, []);

  useEffect(() => {
    if (!job || !['queued', 'running'].includes(job.state)) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await voiceRequest<VoiceJob>(`/jobs/${job.id}`);
        const doc = await voiceRequest<VoiceDocument>(`/projects/${job.project_id}`);
        if (disposed || projectRef.current !== job.project_id) return;
        setJob(next);
        if (!dirty.current && !saving.current) { baseline.current = doc; replace(doc); }
        if (['queued', 'running'].includes(next.state)) timer = setTimeout(poll, 1200);
      } catch (e) { if (!disposed) { setError(String(e)); timer = setTimeout(poll, 3000); } }
    };
    timer = setTimeout(poll, 500);
    return () => { disposed = true; clearTimeout(timer); };
  }, [job, replace]);

  const run = async (ids?: string[]) => {
    setBusy(true); setError('');
    try {
      if (!status?.ready || !status.devices.includes(device)) throw new Error('Thiết bị tạo giọng chưa sẵn sàng. Kiểm tra lại bộ tạo giọng.');
      const doc = await save();
      const nextJob = await voiceRequest<VoiceJob>('/jobs', { method: 'POST', body: JSON.stringify({ project_id: doc.project_id, device, clip_ids: ids ?? null }) });
      if (projectRef.current === doc.project_id) setJob(nextJob);
    } catch (e) { setError(String(e)); } finally { setBusy(false); }
  };
  const plan = () => {
    if (!project || !fingerprint || loadedProject !== project) return;
    const prior = current.current?.project_id === project ? current.current : null;
    const existingIds = new Set(prior?.clips.flatMap(c => c.source_cue_ids));
    const added = planVoiceClips(cues.filter(c => !existingIds.has(c.id)));
    const doc: VoiceDocument = prior ?? { schema_version: 1, project_id: project, video_fingerprint: fingerprint,
      revision: 0, profile: DEFAULT_PROFILE, clips: [], pronunciation: {},
      mix: { enabled: true, muted: false, gain: 1, original_gain: 0.25, mode: 'duck' } };
    dirty.current = true; setSaveState('dirty'); replace({ ...doc, clips: [...doc.clips, ...added].sort((a, b) => a.start_ms - b.start_ms) });
    setSelectedId(added[0]?.id ?? doc.clips[0]?.id ?? null);
  };
  const control = async (action: 'pause' | 'cancel' | 'resume') => {
    if (!job) return;
    try { if (action === 'resume' && dirty.current) await save();
      const next = await voiceRequest<VoiceJob>(`/jobs/${job.id}/${action}`, { method: 'POST' });
      if (projectRef.current === job.project_id) setJob(next);
    } catch (e) { setError(String(e)); }
  };
  const history = (direction: 'undo' | 'redo') => {
    const from = direction === 'undo' ? undo : redo;
    const to = direction === 'undo' ? redo : undo;
    const next = from.current.pop();
    if (next && current.current) { to.current.push(current.current); dirty.current = true;
      setSaveState('dirty'); replace({ ...next, revision: current.current.revision }); }
  };
  return { document: document?.project_id === project ? document : null, status, job, error, setError,
    conflict, resolveConflict,
    selectedId, setSelectedId, device, setDevice, busy, loading: !project || loadedProject !== project,
    retryLoad: () => { setError(''); setLoadAttempt(value => value + 1); },
    saveState, edit, replace: acceptServerDocument, plan, save, run, control, history, refreshStatus };
}
export type VoiceController = ReturnType<typeof useVoiceover>;
