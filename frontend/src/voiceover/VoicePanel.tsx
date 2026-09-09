import { useCallback, useEffect, useRef, useState } from 'react';
import { AudioLines, Play, RefreshCw, Square, Upload, Download } from 'lucide-react';
import { voiceDownload, voiceFetch, voiceRequest } from './api';
import type { VoiceController } from './useVoiceover';
import { voiceClipsInRange } from './planner';
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
  const chooseProfile = (next: VoiceProfile) => voice.edit(current => current.project_id !== doc?.project_id ? current : ({ ...current, profile: next,
    clips: current.clips.map(c => ({ ...c, status: c.asset_id ? 'stale' : 'missing' })) }));
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
    <div className="voice-runtime"><span>{status?.message ?? 'Đang kiểm tra bộ tạo giọng…'}</span>
      <button type="button" aria-label="Kiểm tra lại bộ tạo giọng" onClick={() => void voice.refreshStatus()}><RefreshCw size={16} /></button></div>
    {!status?.ready && <code className="voice-setup-command">.\scripts\setup-voiceover.ps1</code>}
    <button type="button" className="studio-primary-button" disabled={voice.loading} onClick={voice.plan}>Tạo đoạn từ phụ đề</button>
    <small>Mỗi phụ đề có một đoạn giọng riêng, bắt đầu tại mốc của phụ đề.</small>
    {doc?.clips.some(clip => clip.source_cue_ids.length > 1) && <div>
      <p>Có đoạn giọng gộp nhiều phụ đề nên các câu bên trong có thể được đọc sớm. Tách để căn từng câu, rồi bấm “Tạo phần còn thiếu”. Các đoạn vừa tách cần tạo lại audio.</p>
      <button type="button" disabled={voice.loading || voice.busy || working || Boolean(running) || voice.saveState === 'saving'}
        onClick={voice.splitGrouped}>Tách theo từng phụ đề</button>
    </div>}
    {!doc && <p>Nhập video và phụ đề, sau đó tạo các đoạn lời đọc để bắt đầu.</p>}
    <label>Giọng đọc<select value={profile.id} disabled={!doc || working} onChange={e => {
      const saved = profiles.find(p => p.id === e.target.value);
      const preset = status?.presets.find(p => `preset-${p.id}` === e.target.value);
      if (saved) chooseProfile(saved);
      else if (preset) chooseProfile({ ...DEFAULT_PROFILE, id: `preset-${preset.id}`.replace(/[^a-zA-Z0-9_-]/g, '_').slice(0, 80), name: preset.name, preset: preset.id });
    }}>
      <option value={profile.id}>{profile.name}</option>
      {status?.presets.filter(p => p.id !== profile.preset).map(p => <option key={p.id} value={`preset-${p.id}`}>{p.name}</option>)}
      {profiles.filter(p => p.id !== profile.id).map(p => <option key={p.id} value={p.id}>{p.name} · đã lưu</option>)}
    </select></label>
    <label>Chạy trên<select value={voice.device} onChange={e => voice.setDevice(e.target.value as 'cpu' | 'cuda')}>
      <option value="cpu" disabled={!status?.devices.includes('cpu')}>CPU · nghe thử / tạo giọng</option>
      <option value="cuda" disabled={!status?.devices.includes('cuda')}>NVIDIA GPU · xử lý nhiều đoạn</option>
    </select></label>
    <details><summary>Tạo hồ sơ giọng từ mẫu</summary>
      <label>Tên giọng<input value={profileName} maxLength={100} onChange={e => setProfileName(e.target.value)} /></label>
      <p>Mẫu sạch 3–8 giây, một người nói; dùng mẫu bạn có quyền nhân bản. Chỉ lấy tối đa 8 giây đầu.</p>
      <label className="studio-secondary-button"><Upload size={16} /> Chọn mẫu audio
        <input type="file" accept="audio/*" disabled={working || !doc} onChange={e => {
          const file = e.target.files?.[0]; e.target.value = ''; if (!file) return;
          void action(async () => {
            const data = new FormData(); data.append('file', file);
            const reference = await voiceRequest<{ id: string; duration_ms: number }>('/references', { method: 'POST', body: data });
            const response = await voiceFetch(`/references/${reference.id}`);
            const blob = await response.blob();
            if (!mounted.current) return;
            const processed = URL.createObjectURL(blob);
            const original = URL.createObjectURL(file);
            referenceUrls.current.forEach(url => URL.revokeObjectURL(url));
            referenceUrls.current = [original, processed];
            setReferenceSample({ ...reference, original, processed });
          });
        }} />
      </label>
      {referenceSample && <>
        <label>Mẫu gốc<audio controls preload="metadata" src={referenceSample.original} /></label>
        <label>Mẫu dùng tạo giọng · {(referenceSample.duration_ms / 1000).toFixed(1)} giây
          <audio controls preload="metadata" src={referenceSample.processed} /></label>
        <button type="button" disabled={working || !doc || !profileName.trim()} onClick={() => void action(async () => {
          const saved = await voiceRequest<VoiceProfile>('/profiles', { method: 'POST', body: JSON.stringify({
            ...DEFAULT_PROFILE, id: crypto.randomUUID(), name: profileName.trim(), preset: null, reference_id: referenceSample.id,
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
      <button type="button" className="studio-secondary-button" disabled={!status?.ready || working || Boolean(previewRunning) || !previewText.trim()}
        onClick={() => void action(async () => setPreviewJob(await voiceRequest<VoiceJob>('/preview', { method: 'POST',
          body: JSON.stringify({ profile, text: previewText, device: voice.device }) })))}><Play size={16} /> Tạo mẫu nghe</button>
      {previewJob && <p role="status">{previewJob.message}</p>}
      {previewRunning && <button type="button" onClick={() => void action(async () => setPreviewJob(await voiceRequest<VoiceJob>(`/jobs/${previewJob.id}/cancel`, { method: 'POST' })))}>Hủy mẫu nghe</button>}
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
        <button type="button" disabled={!status?.ready || working || voice.busy || Boolean(running) || !rangeIds.length}
          onClick={() => void voice.run(rangeIds)}>Tạo {rangeIds.length} đoạn trong khoảng</button>
      </details>
      <div className="voice-actions">
        <button type="button" className="studio-primary-button" disabled={!status?.ready || voice.busy || Boolean(running) || working || !doc.clips.length} onClick={() => void voice.run()}>Tạo phần còn thiếu</button>
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
      {selected && <fieldset><legend>Đoạn đang chọn · {VOICE_STATUS_LABELS[selected.status]}</legend>
        <label>Lời đọc<textarea rows={5} value={selected.spoken_text} maxLength={8000} onChange={e => voice.edit(d => ({ ...d, clips: d.clips.map(c => c.id === selected.id ? { ...c, spoken_text: e.target.value, status: c.asset_id ? 'stale' : 'missing' } : c) }))} /></label>
        <div className="voice-fields"><label>Tốc độ<input type="number" min={0.5} max={2} step={0.01} value={selected.rate} onChange={e => {
          const rate = Number(e.target.value); if (rate >= 0.5 && rate <= 2) voice.edit(d => ({ ...d, clips: d.clips.map(c => c.id === selected.id ? { ...c, rate } : c) }));
        }} /></label>
        <label>Độ lệch (ms)<input type="number" step={10} min={-selected.start_ms} value={selected.offset_ms} onChange={e => {
          const offset_ms = Number(e.target.value); if (Number.isInteger(offset_ms) && offset_ms >= -selected.start_ms) voice.edit(d => ({ ...d, clips: d.clips.map(c => c.id === selected.id ? { ...c, offset_ms } : c) }));
        }} /></label></div>
        <label>Âm lượng đoạn<input type="range" min={0} max={2} step={0.05} value={selected.gain} onChange={e => voice.edit(d => ({ ...d, clips: d.clips.map(c => c.id === selected.id ? { ...c, gain: Number(e.target.value) } : c) }))} /></label>
        {selected.error && <p className="voice-error">{selected.error}</p>}
        <div className="voice-actions">
          <button type="button" disabled={!selected.asset_id || working} onClick={() => void action(() => listen(selected.asset_id!))}>Nghe riêng</button>
          <button type="button" disabled={!status?.ready || Boolean(running) || voice.busy} onClick={() => void voice.run([selected.id])}>Tạo đoạn này</button>
          <button type="button" disabled={selected.offset_ms === 0} onClick={() => voice.edit(d => ({ ...d,
            clips: d.clips.map(c => c.id === selected.id ? { ...c, offset_ms: 0 } : c),
          }))}>Về mốc phụ đề</button>
          <button type="button" onClick={() => voice.edit(d => ({ ...d, clips: d.clips.filter(c => c.id !== selected.id) }))}>Bỏ đoạn giọng</button>
        </div>
      </fieldset>}
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
