import { useCallback, useEffect, useRef, useState } from 'react';
import { AlertTriangle, AudioLines, Download, Play, RefreshCw, Sparkles, Square, Upload, Zap } from 'lucide-react';
import { voiceDownload, voiceFetch, voiceRequest } from './api';
import type { VoiceController } from './useVoiceover';
import {
  autoFitVoiceClips,
  calculateFitRate,
  countVoiceOverlaps,
  resetVoiceOffsets,
  rippleShiftVoiceClips,
  smartResolveVoiceOverlaps,
  voiceClipsInRange,
} from './planner';
import { DEFAULT_PROFILE, VOICE_STATUS_LABELS, type VoiceDocument, type VoiceJob, type VoiceProfile } from './types';

function describeVoiceDocument(doc: VoiceDocument | null): string {
  if (!doc) return 'Chưa có lời đọc';
  return [`Giọng: ${doc.profile.name}`, `Âm lượng giọng: ${doc.mix.gain}; âm gốc: ${doc.mix.original_gain}`,
    `Chế độ: ${{ voice: 'Chỉ giọng đọc', mix: 'Trộn âm gốc', duck: 'Giảm âm gốc khi đọc' }[doc.mix.mode]}`,
    `Bật giọng: ${doc.mix.enabled ? 'Có' : 'Không'}; tắt tiếng: ${doc.mix.muted ? 'Có' : 'Không'}`,
    `Phát âm: ${Object.entries(doc.pronunciation).map(([word, reading]) => `${word} → ${reading}`).join('; ')}`,
    ...doc.clips.map((clip, index) => `\nĐoạn ${index + 1} · ${((clip.start_ms + clip.offset_ms) / 1000).toFixed(3)} giây · tốc độ ${clip.rate}x · âm lượng ${clip.gain}\n${clip.spoken_text}`),
  ].join('\n');
}

export function VoicePanel({ voice, exportTimeline }: { voice: VoiceController; exportTimeline?: {
  duration_ms: number; trim_start_ms?: number; trim_end_ms?: number | null;
  video_speed?: number; video_segments?: { start_ms: number; end_ms: number }[];
} }) {
  const { document: doc, status, job, selectedId, setError } = voice;
  const selected = doc?.clips.find(c => c.id === selectedId);
  const [profiles, setProfiles] = useState<VoiceProfile[]>([]);
  const [previewText, setPreviewText] = useState('Cô gái tưởng rằng mình đã thoát khỏi nguy hiểm. Nhưng ngay khi cánh cửa mở ra, một bí mật khiến mọi thứ thay đổi.');
  const [previewJob, setPreviewJob] = useState<VoiceJob | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [profileName, setProfileName] = useState('Giọng của tôi');
  const [referenceFile, setReferenceFile] = useState<File | null>(null);
  const [referenceStart, setReferenceStart] = useState('0');
  const [referenceDuration, setReferenceDuration] = useState('8');
  const [referenceDenoise, setReferenceDenoise] = useState(false);
  const [referenceSample, setReferenceSample] = useState<{ id: string; original: string; processed: string; duration_ms: number } | null>(null);
  const referenceUrls = useRef<string[]>([]);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const [working, setWorking] = useState(false);
  const [audioFormat, setAudioFormat] = useState('wav');
  const [rangeStart, setRangeStart] = useState('0');
  const [rangeEnd, setRangeEnd] = useState('60');
  const rangeIds = doc && rangeStart.trim() && rangeEnd.trim()
    ? voiceClipsInRange(doc.clips, Number(rangeStart) * 1000, Number(rangeEnd) * 1000) : [];
  const [pronunciationWord, setPronunciationWord] = useState('');
  const [pronunciationReading, setPronunciationReading] = useState('');
  const urlRef = useRef<string | null>(null);
  const running = job && ['queued', 'running'].includes(job.state);
  const previewRunning = previewJob && ['queued', 'running'].includes(previewJob.state);
  const profile = doc?.profile ?? DEFAULT_PROFILE;
  const engineStatus = status?.engines?.find(engine => engine.model_id === profile.model_id);
  const engineReady = engineStatus?.ready ?? status?.ready;
  const engineDevices = engineStatus?.devices ?? status?.devices ?? [];
  const enginePresets = engineStatus?.presets ?? status?.presets ?? [];
  const validReferenceRange = referenceStart.trim() !== '' && referenceDuration.trim() !== ''
    && Number.isFinite(Number(referenceStart)) && Number(referenceStart) >= 0 && Number(referenceStart) <= 86400
    && Number(referenceDuration) >= 3 && Number(referenceDuration) <= 8;
  const { overflowCount, overlapCount } = doc ? countVoiceOverlaps(doc.clips) : { overflowCount: 0, overlapCount: 0 };
  const hasTimingIssues = overflowCount > 0 || overlapCount > 0;
  useEffect(() => {
    const controller = new AbortController();
    void voiceRequest<VoiceProfile[]>('/profiles', { signal: controller.signal }).then(setProfiles)
      .catch(e => { if (!controller.signal.aborted) setError(String(e)); });
    return () => controller.abort();
  }, [setError]);
  useEffect(() => () => { if (urlRef.current) URL.revokeObjectURL(urlRef.current); }, []);
  useEffect(() => () => { referenceUrls.current.forEach(url => URL.revokeObjectURL(url)); }, []);
  const listen = useCallback(async (asset: string) => {
    const response = await voiceFetch(`/assets/${asset}`);
    const blob = await response.blob();
    if (!mounted.current) return;
    if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    urlRef.current = URL.createObjectURL(blob); setPreviewUrl(urlRef.current);
  }, []);
  useEffect(() => {
    if (!previewJob || !['queued', 'running'].includes(previewJob.state)) return;
    let disposed = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const next = await voiceRequest<VoiceJob>(`/jobs/${previewJob.id}`);
        if (disposed) return;
        if (next.state === 'succeeded') {
          const result = await voiceRequest<VoiceDocument>(`/projects/${next.project_id}`);
          if (!disposed && result.clips[0]?.asset_id) await listen(result.clips[0].asset_id);
        } else if (['queued', 'running'].includes(next.state)) timer = setTimeout(poll, 1200);
        if (!disposed) setPreviewJob(next);
      } catch (e) { if (!disposed) { setError(String(e)); timer = setTimeout(poll, 3000); } }
    };
    timer = setTimeout(poll, 1000);
    return () => { disposed = true; clearTimeout(timer); };
  }, [previewJob, setError, listen]);
  const action = async (fn: () => Promise<unknown>) => {
    setWorking(true); voice.setError('');
    try { await fn(); } catch (e) { if (mounted.current) voice.setError(String(e)); } finally { if (mounted.current) setWorking(false); }
  };
  const chooseProfile = (next: VoiceProfile) => {
    const devices = status?.engines?.find(engine => engine.model_id === next.model_id)?.devices;
    if (devices?.length && !devices.includes(voice.device)) voice.setDevice(devices[0] as 'cpu' | 'cuda');
    setPreviewUrl(null);
    voice.edit(current => current.project_id !== doc?.project_id ? current : ({ ...current, profile: next,
      clips: current.clips.map(c => ({ ...c, status: c.asset_id ? 'stale' : 'missing' })) }));
  };
  const uploadReference = async (file: File) => {
    setReferenceSample(null);
    referenceUrls.current.forEach(url => URL.revokeObjectURL(url)); referenceUrls.current = [];
    const data = new FormData(); data.append('file', file);
    const query = new URLSearchParams({ start_seconds: referenceStart, duration_seconds: referenceDuration });
    const reference = await voiceRequest<{ id: string; duration_ms: number }>(`/references?${query}`, { method: 'POST', body: data });
    const response = await voiceFetch(`/references/${reference.id}`);
    const blob = await response.blob();
    if (!mounted.current) return;
    const processed = URL.createObjectURL(blob), original = URL.createObjectURL(file);
    referenceUrls.current = [original, processed];
    setReferenceSample({ ...reference, original, processed });
  };
  const listenSelected = () => {
    const asset = selected?.asset_id;
    if (asset) void action(() => listen(asset));
  };
  return <div className="studio-panel-section voice-panel">
    <div className="studio-panel-heading"><h2><AudioLines size={18} /> Giọng đọc AI</h2>
      <p>Giọng kể chuyện chạy trên máy. Chọn mẫu nghe trước khi tạo cả phim.</p></div>
    {voice.error && <p role="alert" className="voice-error">{voice.error}</p>}
    {voice.conflict && <fieldset><legend>Đối chiếu thay đổi trùng nhau</legend>
      <p>Các thay đổi độc lập vẫn được giữ. Chọn bản dùng cho những mục cả hai nơi cùng sửa, rồi lưu lại.</p>
      <details><summary>Xem hai phiên bản</summary>
        <label>Bản trên màn hình<textarea readOnly rows={8} value={describeVoiceDocument(doc)} /></label>
        <label>Bản trên máy chủ<textarea readOnly rows={8} value={describeVoiceDocument(voice.conflict.remote)} /></label>
      </details>
      <button type="button" onClick={() => voice.resolveConflict('local')}>Giữ phần sửa của tôi</button>
      <button type="button" onClick={() => voice.resolveConflict('remote')}>Dùng phần sửa từ máy chủ</button>
    </fieldset>}
    {voice.error && voice.loading && <button type="button" onClick={voice.retryLoad}>Thử tải lại dự án giọng đọc</button>}
    <div className="voice-runtime"><span>{engineStatus?.message ?? status?.message ?? 'Đang kiểm tra bộ tạo giọng…'}</span>
      <button type="button" aria-label="Kiểm tra lại bộ tạo giọng" onClick={() => void voice.refreshStatus()}><RefreshCw size={16} /></button></div>
    {!engineReady && <code className="voice-setup-command">.\{engineStatus?.setup_command ?? 'scripts/setup-voiceover.ps1'}</code>}
    <button type="button" className="studio-primary-button" disabled={voice.loading} onClick={voice.plan}>Tạo đoạn từ phụ đề</button>
    <small>Mỗi phụ đề có một đoạn giọng riêng, bắt đầu tại mốc của phụ đề.</small>
    {hasTimingIssues && <div className="voice-warning-banner" role="alert" style={{
      background: '#fff8eb', border: '1px solid #f79009', borderRadius: 6, padding: '10px 12px',
      color: '#7a2e0e', display: 'flex', flexDirection: 'column', gap: 8,
    }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontWeight: 600, fontSize: 13 }}>
        <AlertTriangle size={16} color="#d97706" />
        <span>
          {overflowCount > 0 && overlapCount > 0
            ? `Phát hiện ${overflowCount} đoạn vượt thời lượng và ${overlapCount} điểm chồng lấn`
            : overflowCount > 0
            ? `Phát hiện ${overflowCount} đoạn vượt thời lượng phụ đề`
            : `Phát hiện ${overlapCount} điểm đoạn giọng đè lên nhau`}
        </span>
      </div>
      <p style={{ margin: 0, fontSize: 12, lineHeight: 1.4 }}>
        Các đoạn đè lên nhau hoặc vượt khung phụ đề gây giật tiếng khi phát và bị chặn khi xuất video. Chọn cách xử lý tự động:
      </p>
      <div className="voice-actions" style={{ marginTop: 2 }}>
        <button type="button" className="studio-secondary-button"
          title="Tự động tăng tốc độ cho các đoạn vượt để nằm vừa khung phụ đề, hết đè nhau"
          disabled={working || voice.busy || Boolean(running)}
          onClick={() => voice.edit(d => ({ ...d, clips: autoFitVoiceClips(d.clips) }))}>
          <Zap size={14} /> Tự động tăng tốc vừa khung
        </button>
        <button type="button"
          title="Dịch mốc bắt đầu của các câu sau để các câu nối tiếp tuần tự, không bị nói đè lên nhau"
          disabled={working || voice.busy || Boolean(running)}
          onClick={() => voice.edit(d => ({ ...d, clips: rippleShiftVoiceClips(d.clips) }))}>
          Dịch mốc tránh đè
        </button>
        <button type="button"
          title="Tăng tốc vừa phải kết hợp dịch mốc vào khoảng lặng để giọng đọc tự nhiên và không đè nhau"
          disabled={working || voice.busy || Boolean(running)}
          onClick={() => voice.edit(d => ({ ...d, clips: smartResolveVoiceOverlaps(d.clips) }))}>
          <Sparkles size={14} /> Tối ưu thông minh
        </button>
      </div>
    </div>}
    {doc?.clips.some(clip => clip.source_cue_ids.length > 1) && <div>
      <p>Có đoạn giọng gộp nhiều phụ đề nên các câu bên trong có thể được đọc sớm. Tách để căn từng câu, rồi bấm “Tạo phần còn thiếu”. Các đoạn vừa tách cần tạo lại audio.</p>
      <button type="button" disabled={voice.loading || voice.busy || working || Boolean(running) || voice.saveState === 'saving'}
        onClick={voice.splitGrouped}>Tách theo từng phụ đề</button>
    </div>}
    {!doc && <p>Nhập video và phụ đề, sau đó tạo các đoạn lời đọc để bắt đầu.</p>}
    {status?.engines && <label>Engine tạo giọng<select value={profile.model_id}
      disabled={!doc || working || Boolean(running) || Boolean(previewRunning)} onChange={e => {
        const engine = status.engines?.find(item => item.model_id === e.target.value);
        if (!engine) return;
        chooseProfile({ ...profile, id: crypto.randomUUID(), revision: 1,
          model_id: engine.model_id, model_revision: engine.model_revision,
          preset: profile.reference_id ? null : engine.presets[0]?.id ?? '__default__',
          name: profile.reference_id ? profile.name : engine.presets[0]?.name ?? engine.name });
      }}>
      {status.engines.map(engine => <option key={engine.model_id} value={engine.model_id}>
        {engine.name}{engine.ready ? '' : ' · cần cài đặt'}
      </option>)}
    </select></label>}
    <label>Giọng đọc<select value={profile.id} disabled={!doc || working || Boolean(previewRunning)} onChange={e => {
      const saved = profiles.find(p => p.id === e.target.value);
      const preset = enginePresets.find(p => `preset-${p.id}` === e.target.value);
      if (saved) chooseProfile(saved);
      else if (preset) chooseProfile({ ...DEFAULT_PROFILE, model_id: profile.model_id, model_revision: profile.model_revision,
        id: `preset-${preset.id}`.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 80), name: preset.name, preset: preset.id });
    }}>
      <option value={profile.id}>{profile.name}</option>
      {enginePresets.filter(p => p.id !== profile.preset).map(p => <option key={p.id} value={`preset-${p.id}`}>{p.name}</option>)}
      {profiles.filter(p => p.id !== profile.id).map(p => <option key={p.id} value={p.id}>{p.name} · đã lưu</option>)}
    </select></label>
    {profile.reference_id && <label>Xử lý mẫu giọng đang dùng<select value={String(profile.denoise ?? true)}
      disabled={working || Boolean(running) || Boolean(previewRunning)}
      onChange={e => chooseProfile({ ...profile, denoise: e.target.value === 'true' })}>
      <option value="false">Giữ âm gốc · mẫu đã sạch</option>
      <option value="true">Lọc nhiễu · mẫu có tiếng nền</option>
    </select><small>Đổi chế độ rồi bấm “Tạo mẫu nghe” để so sánh. Lựa chọn được lưu cùng dự án.</small></label>}
    <label>Chạy trên<select value={voice.device} onChange={e => voice.setDevice(e.target.value as 'cpu' | 'cuda')}>
      <option value="cpu" disabled={!engineDevices.includes('cpu')}>CPU · nghe thử / tạo giọng</option>
      <option value="cuda" disabled={!engineDevices.includes('cuda')}>NVIDIA GPU · xử lý nhiều đoạn</option>
    </select></label>
    <details><summary>Tạo hồ sơ giọng từ mẫu</summary>
      <label>Tên giọng<input value={profileName} maxLength={100} onChange={e => setProfileName(e.target.value)} /></label>
      <p>Chọn 3–8 giây nói rõ, một người, ít nhạc và tiếng vang. Dùng mẫu bạn có quyền nhân bản.</p>
      <div className="voice-fields">
        <label>Bắt đầu tại giây<input type="number" min={0} max={86400} step={0.1} value={referenceStart} disabled={working}
          onChange={e => { setReferenceStart(e.target.value); setReferenceSample(null); }} /></label>
        <label>Lấy số giây<input type="number" min={3} max={8} step={0.1} value={referenceDuration} disabled={working}
          onChange={e => { setReferenceDuration(e.target.value); setReferenceSample(null); }} /></label>
      </div>
      <label>Xử lý mẫu mới<select value={String(referenceDenoise)} disabled={working}
        onChange={e => setReferenceDenoise(e.target.value === 'true')}>
        <option value="false">Giữ âm gốc · mẫu đã sạch</option>
        <option value="true">Lọc nhiễu · mẫu có tiếng nền</option>
      </select></label>
      <label className="studio-secondary-button"><Upload size={16} /> Chọn mẫu audio
        <input type="file" accept="audio/*" disabled={working || !doc || !validReferenceRange} onChange={e => {
          const file = e.target.files?.[0]; e.target.value = ''; if (!file) return;
          setReferenceFile(file);
          void action(() => uploadReference(file));
        }} />
      </label>
      {referenceFile && <button type="button" disabled={working || !doc || !validReferenceRange}
        onClick={() => void action(() => uploadReference(referenceFile))}>Lấy lại đoạn mẫu · {referenceFile.name}</button>}
      {referenceSample && <>
        <label>Mẫu gốc<audio controls preload="metadata" src={referenceSample.original} /></label>
        <label>Đoạn đã cắt · {(referenceSample.duration_ms / 1000).toFixed(1)} giây · trước lọc nhiễu
          <audio controls preload="metadata" src={referenceSample.processed} /></label>
        <button type="button" disabled={working || !doc || !profileName.trim()} onClick={() => void action(async () => {
          const saved = await voiceRequest<VoiceProfile>('/profiles', { method: 'POST', body: JSON.stringify({
            ...DEFAULT_PROFILE, id: crypto.randomUUID(), name: profileName.trim(), preset: null, reference_id: referenceSample.id,
            model_id: profile.model_id, model_revision: profile.model_revision, denoise: referenceDenoise,
          }) });
          if (!mounted.current) return;
          setProfiles(current => [...current, saved]); chooseProfile(saved);
          setReferenceSample(null);
          referenceUrls.current.forEach(url => URL.revokeObjectURL(url)); referenceUrls.current = [];
        })}>Lưu và dùng mẫu giọng này</button>
      </>}
    </details>
    <details open><summary>Nghe thử chất giọng</summary>
      <label>Lời đọc thử<textarea rows={4} value={previewText} maxLength={3000} onChange={e => setPreviewText(e.target.value)} /></label>
      <button type="button" className="studio-secondary-button" disabled={!engineReady || !engineDevices.includes(voice.device) || working || Boolean(previewRunning) || !previewText.trim()}
        onClick={() => void action(async () => setPreviewJob(await voiceRequest<VoiceJob>('/preview', { method: 'POST',
          body: JSON.stringify({ profile, text: previewText, device: voice.device }) })))}><Play size={16} /> Tạo mẫu nghe</button>
      {previewJob && <p role="status">{previewJob.state === 'interrupted'
        ? 'Tác vụ bị gián đoạn khi bộ tạo giọng khởi động lại. Bấm “Tiếp tục mẫu nghe” để chạy lại.'
        : previewJob.message}</p>}
      {previewRunning && <button type="button" onClick={() => void action(async () => setPreviewJob(await voiceRequest<VoiceJob>(`/jobs/${previewJob.id}/cancel`, { method: 'POST' })))}>Hủy mẫu nghe</button>}
      {previewJob && ['interrupted', 'paused', 'failed', 'canceled'].includes(previewJob.state) &&
        <button type="button" disabled={working} onClick={() => void action(async () => {
          setPreviewUrl(null);
          setPreviewJob(await voiceRequest<VoiceJob>(`/jobs/${previewJob.id}/resume`, { method: 'POST' }));
        })}>Tiếp tục mẫu nghe</button>}
      {previewUrl && <audio controls src={previewUrl} preload="metadata" aria-label="Nghe giọng đã tạo" />}
    </details>
    {doc && <>
      <small role="status">{{ saved: 'Đã lưu lời đọc', dirty: 'Có thay đổi chưa lưu', saving: 'Đang lưu…', error: 'Chưa lưu được. Kiểm tra thông báo lỗi.' }[voice.saveState]}</small>
      <details><summary>Tạo giọng theo khoảng thời gian</summary>
        <div className="voice-fields">
          <label>Từ giây<input type="number" min={0} step={0.001} value={rangeStart} onChange={e => setRangeStart(e.target.value)} /></label>
          <label>Đến giây<input type="number" min={0} step={0.001} value={rangeEnd} onChange={e => setRangeEnd(e.target.value)} /></label>
        </div>
        <p>{rangeIds.length} đoạn giao với khoảng chọn. Tạo nguyên câu, kể cả câu kéo qua biên; dùng lại audio còn hợp lệ.</p>
        <button type="button" disabled={!engineReady || !engineDevices.includes(voice.device) || working || voice.busy || Boolean(running) || !rangeIds.length}
          onClick={() => void voice.run(rangeIds)}>Tạo {rangeIds.length} đoạn trong khoảng</button>
      </details>
      <div className="voice-actions">
        <button type="button" className="studio-primary-button" disabled={!engineReady || !engineDevices.includes(voice.device) || voice.busy || Boolean(running) || working || !doc.clips.length} onClick={() => void voice.run()}>Tạo phần còn thiếu</button>
        <button type="button" disabled={working || voice.busy} onClick={() => void action(voice.save)}>Lưu lời đọc</button>
        <button type="button" onClick={() => voice.history('undo')}>Hoàn tác</button>
        <button type="button" onClick={() => voice.history('redo')}>Làm lại</button>
      </div>
      {job && <div className="voice-job" role="status"><progress value={job.completed} max={Math.max(1, job.total)} />
        <span>{job.completed}/{job.total} đoạn · {job.message}</span>
        {job.eta_seconds !== null && running && <small>Còn khoảng {Math.ceil(job.eta_seconds / 60)} phút</small>}
        <div className="voice-actions">{running ? <>
          <button type="button" onClick={() => void voice.control('pause')}>Tạm dừng</button>
          <button type="button" onClick={() => void voice.control('cancel')}><Square size={14} /> Hủy</button>
        </> : job.state !== 'succeeded' && <button type="button" onClick={() => void voice.control('resume')}>Tiếp tục</button>}</div>
        {job.failed.map(f => <small key={f.clip_id}>{f.error}</small>)}
      </div>}
      {selected && (() => {
        const neededRate = selected.duration_ms && (selected.end_ms - selected.start_ms > 0)
          ? calculateFitRate(selected)
          : null;
        const canFit = Boolean(neededRate && Math.abs(neededRate - selected.rate) > 0.005);
        return <fieldset><legend>Đoạn đang chọn · {VOICE_STATUS_LABELS[selected.status]}</legend>
        <label>Lời đọc<textarea rows={5} value={selected.spoken_text} maxLength={8000} onChange={e => voice.edit(d => ({ ...d, clips: d.clips.map(c => c.id === selected.id ? { ...c, spoken_text: e.target.value, status: c.asset_id ? 'stale' : 'missing' } : c) }))} /></label>
        <div className="voice-fields"><label>Tốc độ<input type="number" min={0.5} max={2} step={0.01} value={selected.rate} onChange={e => {
          const rate = Number(e.target.value); if (rate >= 0.5 && rate <= 2) voice.edit(d => ({ ...d, clips: d.clips.map(c => c.id === selected.id ? { ...c, rate } : c) }));
        }} /></label>
        <label>Độ lệch (ms)<input type="number" step={10} min={-selected.start_ms} value={selected.offset_ms} onChange={e => {
          const offset_ms = Number(e.target.value); if (Number.isInteger(offset_ms) && offset_ms >= -selected.start_ms) voice.edit(d => ({ ...d, clips: d.clips.map(c => c.id === selected.id ? { ...c, offset_ms } : c) }));
        }} /></label></div>
        <label>Âm lượng đoạn<input type="range" min={0} max={2} step={0.05} value={selected.gain} onChange={e => voice.edit(d => ({ ...d, clips: d.clips.map(c => c.id === selected.id ? { ...c, gain: Number(e.target.value) } : c) }))} /></label>
        {selected.error && <p className="voice-error">{selected.error}</p>}
        {selected.status === 'overflow' && neededRate && <small style={{ color: '#b54708', display: 'block' }}>
          Đoạn này đang vượt khung phụ đề. Cần tốc độ tối thiểu {neededRate}x để nằm gọn trong phụ đề.
        </small>}
        <div className="voice-actions">
          <button type="button" disabled={!selected.asset_id || working} onClick={listenSelected}>Nghe riêng</button>
          <button type="button" disabled={!engineReady || !engineDevices.includes(voice.device) || Boolean(running) || voice.busy} onClick={() => void voice.run([selected.id])}>Tạo đoạn này</button>
          {canFit && <button type="button" title={`Tự động đặt tốc độ thành ${neededRate}x để vừa khung phụ đề`} onClick={() => voice.edit(d => ({ ...d,
            clips: d.clips.map(c => c.id === selected.id ? {
              ...c, rate: neededRate!, offset_ms: 0,
              status: (c.duration_ms / neededRate! <= (c.end_ms - c.start_ms) + 2 && c.status === 'overflow') ? 'ready' : c.status,
            } : c),
          }))}><Zap size={14} /> Vừa khung phụ đề ({neededRate}x)</button>}
          <button type="button" disabled={selected.offset_ms === 0} onClick={() => voice.edit(d => ({ ...d,
            clips: d.clips.map(c => c.id === selected.id ? { ...c, offset_ms: 0 } : c),
          }))}>Về mốc phụ đề</button>
          <button type="button" onClick={() => voice.edit(d => ({ ...d, clips: d.clips.filter(c => c.id !== selected.id) }))}>Bỏ đoạn giọng</button>
        </div>
      </fieldset>;
      })()}
      <details><summary>Căn chỉnh & xử lý chồng lấn ({doc.clips.length} đoạn)</summary>
        <p>Tối ưu tốc độ hoặc dịch mốc để tránh các đoạn giọng nói đè lên nhau.</p>
        <div className="voice-actions">
          <button type="button" disabled={working || voice.busy || Boolean(running)}
            onClick={() => voice.edit(d => ({ ...d, clips: autoFitVoiceClips(d.clips) }))}>
            <Zap size={14} /> Tăng tốc vừa khung tất cả
          </button>
          <button type="button" disabled={working || voice.busy || Boolean(running)}
            onClick={() => voice.edit(d => ({ ...d, clips: rippleShiftVoiceClips(d.clips) }))}>
            Dịch mốc tránh đè tất cả
          </button>
          <button type="button" disabled={working || voice.busy || Boolean(running)}
            onClick={() => voice.edit(d => ({ ...d, clips: smartResolveVoiceOverlaps(d.clips) }))}>
            <Sparkles size={14} /> Tối ưu thông minh
          </button>
          <button type="button" disabled={working || voice.busy || Boolean(running) || !doc.clips.some(c => c.offset_ms !== 0)}
            onClick={() => voice.edit(d => ({ ...d, clips: resetVoiceOffsets(d.clips) }))}>
            Đặt lại về mốc phụ đề
          </button>
        </div>
      </details>
      <details><summary>Trộn âm thanh</summary>
        <p>Giảm âm gốc cũng giảm nhạc và hiệu ứng nằm trong cùng bản thu.</p>
        <label><input type="checkbox" checked={doc.mix.enabled} onChange={e => voice.edit(d => ({ ...d, mix: { ...d.mix, enabled: e.target.checked } }))} /> Dùng giọng AI khi xuất</label>
        <label><input type="checkbox" checked={doc.mix.muted} onChange={e => voice.edit(d => ({ ...d, mix: { ...d.mix, muted: e.target.checked } }))} /> Tắt tiếng hàng giọng</label>
        <label>Chế độ<select value={doc.mix.mode} onChange={e => voice.edit(d => ({ ...d, mix: { ...d.mix, mode: e.target.value as 'voice' | 'mix' | 'duck' } }))}>
          <option value="voice">Chỉ giọng AI</option><option value="mix">Trộn cùng âm gốc</option><option value="duck">Giảm âm gốc khi có lời đọc</option></select></label>
        <label>Âm lượng giọng<input type="range" min={0} max={2} step={0.05} value={doc.mix.gain} onChange={e => voice.edit(d => ({ ...d, mix: { ...d.mix, gain: Number(e.target.value) } }))} /></label>
        <label>Âm lượng gốc<input type="range" min={0} max={2} step={0.05} value={doc.mix.original_gain} onChange={e => voice.edit(d => ({ ...d, mix: { ...d.mix, original_gain: Number(e.target.value) } }))} /></label>
      </details>
      <details><summary>Từ điển phát âm</summary>
        <p>Đổi cách đọc tên riêng và chữ viết tắt, giữ nguyên chữ phụ đề.</p>
        <label>Chữ trong lời đọc<input value={pronunciationWord} maxLength={100} onChange={e => setPronunciationWord(e.target.value)} /></label>
        <label>Cách đọc thay thế<input value={pronunciationReading} maxLength={200} onChange={e => setPronunciationReading(e.target.value)} /></label>
        <button type="button" disabled={!pronunciationWord.trim() || !pronunciationReading.trim()} onClick={() => {
          voice.edit(d => ({ ...d, pronunciation: { ...d.pronunciation, [pronunciationWord.trim()]: pronunciationReading.trim() },
            clips: d.clips.map(c => ({ ...c, status: c.asset_id ? 'stale' : 'missing' })) }));
          setPronunciationWord(''); setPronunciationReading('');
        }}>Thêm cách đọc</button>
        {Object.entries(doc.pronunciation).map(([word, reading]) => <div className="voice-actions" key={word}>
          <span>{word} → {reading}</span><button type="button" aria-label={`Xóa cách đọc ${word}`} onClick={() => voice.edit(d => {
            const pronunciation = { ...d.pronunciation }; delete pronunciation[word];
            return { ...d, pronunciation, clips: d.clips.map(c => ({ ...c, status: c.asset_id ? 'stale' : 'missing' })) };
          })}>Xóa</button>
        </div>)}
      </details>
      <details><summary>Kaggle và tải audio</summary><p>Xuất lời đọc và mẫu giọng, chạy notebook rồi nhập gói kết quả. Không cần tải phim lên.</p>
        <div className="voice-actions"><button type="button" disabled={working} onClick={() => void action(async () => {
          await voice.save(); await voiceDownload('/packages/export', 'voiceover-kaggle.zip', { method: 'POST', body: JSON.stringify({ project_id: doc.project_id, device: 'cuda' }) });
        })}><Download size={16} /> Xuất gói Kaggle</button>
        <label className="studio-secondary-button"><Upload size={16} /> Nhập kết quả<input type="file" accept=".zip" disabled={working} onChange={e => {
          const file = e.target.files?.[0]; e.target.value = ''; if (!file) return;
          void action(async () => { await voice.save(); const data = new FormData(); data.append('file', file);
            const result = await voiceRequest<VoiceDocument>(`/packages/import/${doc.project_id}`, { method: 'POST', body: data }); voice.replace(result); });
        }} /></label>
        <label>Định dạng audio<select value={audioFormat} onChange={e => setAudioFormat(e.target.value)}>
          <option value="wav">WAV</option><option value="flac">FLAC</option><option value="mp3">MP3</option>
        </select></label>
        <button type="button" disabled={working} onClick={() => void action(async () => {
          const saved = await voice.save();
          if (exportTimeline) await voiceDownload(`/projects/${doc.project_id}/audio`, `giong-doc.${audioFormat}`, {
            method: 'POST', body: JSON.stringify({ ...exportTimeline, revision: saved.revision, format: audioFormat }),
          });
          else await voiceDownload(`/projects/${doc.project_id}/audio?format=${audioFormat}`, `giong-doc.${audioFormat}`);
        })}>Tải audio {audioFormat.toUpperCase()}</button></div>
        <p>{exportTimeline ? 'Audio giọng đọc áp dụng điểm cắt và tốc độ video hiện tại, cùng âm lượng giọng.' : 'Audio giọng đọc theo timeline gốc, gồm tốc độ và âm lượng giọng.'}</p>
      </details>
    </>}
  </div>;
}
