import { useEffect, useRef, useState } from 'react';
import { request } from '../transport/client';
import type { VoiceController } from './useVoiceover';
import { SyncCandidateAudio } from './SyncCandidateAudio';
import type { VoiceDocument } from './types';
import type { SubtitleCueV2 } from '../subtitles/types';

type AuditRow = {
  clip_id: string; issues: string[]; completed?: boolean; already_aligned?: boolean;
  source_diagnostics?: { matched_units: number; expected_units: number; minimum_confidence: number;
    contiguous_match?: boolean; complete_word_boundaries?: boolean; ambiguous_occurrence?: boolean;
    shared_word_boundary: boolean; observed_text: string }[];
  following_gap?: {start_ms: number; end_ms: number; classification: string};
  source_evidence?: { start_ms: number; end_ms: number; method: string };
  audio?: { protected_head_candidate_ms: number; protected_tail_candidate_ms: number };
  dubbed_audio?: {speech_verified: boolean; automatic_trim_allowed: boolean; trim_start_ms: number;
    trim_end_ms: number; observed_text?: string;
    diagnostics?: {transcript_matches: boolean; minimum_confidence: number; low_confidence_words: string[]} };
  proposal?: { required_rate: number | null; suggested_rate: number; suggested_offset_ms: number;
    blocked_reasons: string[]; state: string };
  processed?: { state: string; issues: string[]; audio: {id: string; duration_ms: number};
    speech_start_ms?: number; speech_end_ms?: number };
  processing_error?: string;
};
type AuditResponse = {
  audit_id: string;
  job: { state: string; progress: number; message: string; error?: string | null } | null;
  audit?: { rows: AuditRow[]; warnings: { code: string; message: string; cue_id?: string }[];
    voice_changed_during_audit?: boolean; applied_clip_ids?: string[]; applied_proofs?: Record<string, string> } | null;
};
const reasons: Record<string, string> = {
  source_unverified: 'Chưa căn được lời nói nguồn', source_mapping_unresolved: 'Cần kiểm tra liên kết phụ đề',
  source_audio_budget: 'Ngoài ngân sách audio lượt này', timing_locked: 'Mốc và tốc độ đang khóa',
  audio_not_ready: 'Audio chưa sẵn sàng', audio_stale: 'Audio không còn khớp lời đọc',
  source_overlap: 'Vùng nói chồng lượt tiếp theo', duration_not_feasible: 'Giọng còn quá dài ở tốc độ 1,15×',
  existing_fast_rate_needs_review: 'Tốc độ cũ quá cao, cần nghe kiểm tra',
  speech_in_gap_candidate: 'Khoảng trống có dấu hiệu lời nói; cần kiểm tra nội dung bị thiếu',
  quiet_gap_candidate: 'Khoảng trống có dấu hiệu yên; chưa đủ bằng chứng để mượn thời gian',
  unobserved_gap: 'Chưa quan sát đủ khoảng trống này',
  dubbed_audio_budget: 'Chưa kiểm tra lời đọc vì hết ngân sách lượt này',
  dubbed_speech_missing: 'Không phát hiện tín hiệu giọng để kiểm tra',
  dubbed_transcript_unverified: 'Lời nhận dạng từ WAV chưa đủ khớp lời đọc',
  dubbed_activity_unverified: 'Chưa xác định được vùng giọng trong WAV',
  processed_audio_budget: 'Chưa tạo bản căn thời gian vì hết ngân sách lượt này',
  processed_audio_failed: 'Không tạo được bản audio căn thời gian',
  processed_speech_ends_late: 'Sau xử lý, lời đọc vẫn vượt vùng nói cho phép',
  outside_video: 'Audio vượt biên video', overlap: 'Audio chồng đoạn lân cận',
};

function auditInputs(doc: VoiceDocument, cues: readonly SubtitleCueV2[]) {
  return JSON.stringify([doc.project_id, doc.video_fingerprint, doc.profile, doc.pronunciation,
    doc.clips.map(clip => [clip.id, clip.source_cue_ids, clip.source_text, clip.spoken_text, clip.start_ms,
      clip.end_ms, clip.offset_ms, clip.rate, clip.gain, clip.asset_id, clip.duration_ms, clip.generation_hash,
      clip.sync?.timing_origin, clip.sync?.timing_locked, clip.sync?.text_locked, clip.sync?.alignment ?? null]), cues]);
}

export function VoiceSyncPanel({ voice }: { voice: VoiceController }) {
  const project = voice.document?.project_id;
  const storageKey = project ? `voice-sync-audit:${project}` : null;
  const [auditId, setAuditId] = useState<string | null>(() => storageKey ? sessionStorage.getItem(storageKey) : null);
  const [result, setResult] = useState<AuditResponse | null>(null);
  const [starting, setStarting] = useState(false);
  const [priorChecked, setPriorChecked] = useState<string[]>(() => {
    try { const value = JSON.parse(sessionStorage.getItem(`${storageKey}:checked`) ?? '[]');
      return Array.isArray(value) ? value.filter(id => typeof id === 'string') : []; } catch { return []; }
  });
  const [error, setError] = useState('');
  const input = useRef<{ doc: VoiceDocument; cues: readonly SubtitleCueV2[]; signature: string } | null>(null);
  const cancelingObsolete = useRef<string | null>(null);
  const mounted = useRef(false);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const active = Boolean(auditId && (!result || result.job && ['queued', 'running'].includes(result.job.state)));
  const generating = Boolean(voice.job && ['queued', 'running'].includes(voice.job.state));
  const autoAuditSeen = useRef<string | null>(null);
  const lastGeneratingJob = useRef<string | null>(null);

  useEffect(() => {
    if (generating && voice.job) lastGeneratingJob.current = voice.job.id;
    const completedId = voice.job && ['succeeded', 'failed'].includes(voice.job.state) ? voice.job.sync_result?.audit_id : null;
    if (!completedId || autoAuditSeen.current === completedId) return;
    autoAuditSeen.current = completedId;
    if (completedId === auditId || (auditId && lastGeneratingJob.current !== voice.job?.id)) return;
    if (storageKey) sessionStorage.setItem(storageKey, completedId);
    setAuditId(completedId); setResult(null);
  }, [voice.job, auditId, storageKey, generating]);

  useEffect(() => {
    if (!active || !auditId || starting || !voice.document || voice.loading || cancelingObsolete.current === auditId) return;
    if (input.current?.doc === voice.document && input.current.cues === voice.sourceCues) return;
    const signature = auditInputs(voice.document, voice.sourceCues);
    if (input.current && signature !== input.current.signature) {
      cancelingObsolete.current = auditId;
      void request(`/subtitles/v2/voice-sync/${auditId}/cancel`, { method: 'POST' }).catch(e => {
        cancelingObsolete.current = null;
        if (mounted.current) setError(String(e));
      });
    } else {
      input.current = { doc: voice.document, cues: voice.sourceCues, signature };
    }
  }, [active, auditId, starting, voice.document, voice.sourceCues, voice.loading]);

  useEffect(() => {
    if (!auditId || !active) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        const value = await request<AuditResponse>(`/subtitles/v2/voice-sync/${auditId}`, { signal: controller.signal });
        if (controller.signal.aborted) return;
        setResult(value); setError('');
        if (value.job && ['queued', 'running'].includes(value.job.state)) timer = setTimeout(poll, 1200);
      } catch (e) {
        if (!controller.signal.aborted) { setError(String(e)); timer = setTimeout(poll, 3000); }
      }
    };
    void poll();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [auditId, active]);

  const start = async (ids: string[]) => {
    if (!ids.length || !project || starting || active) return;
    setStarting(true); setError('');
    try {
      const doc = await voice.save();
      input.current = { doc, cues: voice.sourceCues, signature: auditInputs(doc, voice.sourceCues) };
      cancelingObsolete.current = null;
      const value = await request<AuditResponse>('/subtitles/v2/voice-sync', { method: 'POST', body: JSON.stringify({
        video_id: project, voice_revision: doc.revision, clip_ids: ids, align_source: true,
        document: { schema_version: 2, language: 'vi', timebase: 'milliseconds', segments: voice.sourceCues },
      }) });
      if (storageKey) sessionStorage.setItem(storageKey, value.audit_id);
      const checked = [...new Set([...priorChecked, ...(result?.audit?.rows.filter(row => row.completed).map(row => row.clip_id) ?? [])])];
      if (storageKey) sessionStorage.setItem(`${storageKey}:checked`, JSON.stringify(checked));
      if (mounted.current) { setPriorChecked(checked); setResult(value); setAuditId(value.audit_id); }
    } catch (e) { if (mounted.current) setError(String(e)); }
    finally { if (mounted.current) setStarting(false); }
  };
  const cancel = async () => {
    try {
      await request(`/subtitles/v2/voice-sync/${auditId}/cancel`, { method: 'POST' });
    } catch (e) { if (mounted.current) setError(String(e)); }
  };
  const apply = async () => {
    const ids = result?.audit?.rows.filter(row => row.completed && row.processed?.state === 'ready_for_review')
      .map(row => row.clip_id) ?? [];
    if (!ids.length || !project || !auditId || starting || active) return;
    setStarting(true); setError('');
    try {
      const saved = await voice.save();
      const applied = await request<VoiceDocument>(`/subtitles/v2/voice-sync/${auditId}/apply`, {
        method: 'POST', body: JSON.stringify({ video_id: project, voice_revision: saved.revision, clip_ids: ids,
          document: {schema_version: 2, language: 'vi', timebase: 'milliseconds', segments: voice.sourceCues} }),
      });
      voice.acceptSyncResult(applied);
      if (mounted.current) setResult(previous => previous?.audit
        ? { ...previous, audit: { ...previous.audit, applied_clip_ids: ids, applied_proofs: Object.fromEntries(
          applied.clips.filter(clip => ids.includes(clip.id)).map(clip => [clip.id, clip.sync?.alignment?.proof_id ?? ''])) } } : previous);
    } catch (e) { if (mounted.current) setError(String(e)); }
    finally { if (mounted.current) setStarting(false); }
  };
  if (!voice.document) return null;
  const reviewed = new Set([...priorChecked, ...(result?.audit?.rows.filter(row => row.completed).map(row => row.clip_id) ?? [])]);
  const pending = voice.document.clips.filter(clip => !reviewed.has(clip.id)).slice(0, 40).map(clip => clip.id);
  const selectedRow = result?.audit?.rows.find(row => row.clip_id === voice.selectedId);
  const disabled = starting || active || generating || voice.busy || voice.loading;
  const appliedCount = voice.document.clips.filter(clip => clip.sync?.alignment?.proof_id
    && result?.audit?.applied_proofs?.[clip.id] === clip.sync.alignment.proof_id).length;
  return <details><summary>Kiểm tra độ khớp với lời nói nguồn</summary>
    <p>Căn lời nguồn và đo audio theo từng vùng. Giữ nguyên video, phụ đề và giọng đã tạo trong bước kiểm tra.</p>
    <div className="voice-actions">
      <button type="button" disabled={disabled || !voice.selectedId} onClick={() => void start([voice.selectedId!])}>Kiểm tra đoạn đang chọn</button>
      <button type="button" disabled={disabled || !pending.length} onClick={() => void start(pending)}>Kiểm tra {pending.length} đoạn tiếp</button>
      {active && <button type="button" onClick={() => void cancel()}>Hủy kiểm tra đồng bộ</button>}
      {result?.audit?.rows.some(row => row.completed && row.processed?.state === 'ready_for_review')
        && !appliedCount && <button type="button" disabled={disabled}
          onClick={() => void apply()}>Áp dụng các đoạn đã đạt kiểm tra</button>}
    </div>
    {!!appliedCount && <p>Đã áp dụng {appliedCount} đoạn.
      Nghe trên timeline và xuất dùng cùng WAV đã xử lý; có thể hoàn tác.</p>}
    {result?.job && <p role="status">{result.job.message} · {result.job.progress}%</p>}
    {(error || result?.job?.error) && <p className="voice-error">{error || result?.job?.error}</p>}
    {result && !result.job && <p>Lượt kiểm tra không còn trong hàng đợi; kết quả đã lưu được giữ lại.</p>}
    {result?.audit?.voice_changed_during_audit && <p>Dự án đã thay đổi trong lúc kiểm tra. Cần kiểm tra lại vùng đã sửa trước khi áp dụng.</p>}
    {result?.audit?.warnings.filter(warning => !warning.cue_id).map((warning, index) => <small key={index}>{warning.message}</small>)}
    {result?.audit && <p>Đã lưu kết quả {result.audit.rows.length} đoạn. Chọn một đoạn để xem chi tiết; mốc đề xuất chưa phải xác nhận khớp tuyệt đối.</p>}
    {selectedRow && <div>
      {selectedRow.already_aligned && <p>Đoạn đã căn và đầu vào vẫn khớp; giữ WAV hiện tại, không chạy lại nhận dạng.</p>}
      {result?.audit?.warnings.filter(warning => warning.cue_id && voice.document?.clips.find(
        clip => clip.id === selectedRow.clip_id)?.source_cue_ids.includes(warning.cue_id))
        .map((warning, index) => <p key={index}>{warning.message}</p>)}
      {selectedRow.source_diagnostics?.map((diagnostic, index) => <div key={index}>
        <p>ASR nghe được: {diagnostic.observed_text || '(không có lời nhận dạng)'}</p>
        <p>Ghép được {diagnostic.matched_units}/{diagnostic.expected_units} ký tự nguồn.</p>
        {diagnostic.minimum_confidence < .65 && <p>Có từ nhận dạng chưa chắc chắn; giữ mốc cũ để kiểm tra.</p>}
        {diagnostic.contiguous_match === false && <p>Các chữ khớp bị ngắt bởi lời khác.</p>}
        {diagnostic.complete_word_boundaries === false && <p>ASR chưa tách riêng được âm đầu hoặc âm cuối của câu này.</p>}
        {diagnostic.ambiguous_occurrence && <p>Câu xuất hiện nhiều lần trong vùng nghe; chưa xác định được đúng lượt.</p>}
      </div>)}
      {selectedRow.following_gap && <p>{reasons[selectedRow.following_gap.classification] ?? selectedRow.following_gap.classification}.</p>}
      {selectedRow.source_evidence && <p>Lời nguồn: {(selectedRow.source_evidence.start_ms / 1000).toFixed(3)}–{(selectedRow.source_evidence.end_ms / 1000).toFixed(3)} giây, từ ASR.</p>}
      {[...selectedRow.issues, ...(selectedRow.proposal?.blocked_reasons ?? [])].map((reason, index) => <p key={index}>{reasons[reason] ?? reason}</p>)}
      {selectedRow.proposal?.required_rate != null && <p>Tốc độ cần để vừa vùng nói: {selectedRow.proposal.required_rate.toFixed(2)}×.</p>}
      {selectedRow.audio && <small>Ứng viên phần yên đầu/cuối: {Math.round(selectedRow.audio.protected_head_candidate_ms)} / {Math.round(selectedRow.audio.protected_tail_candidate_ms)} ms. Cần xác minh trước khi cắt.</small>}
      {selectedRow.dubbed_audio && <p>Kiểm tra WAV: {selectedRow.dubbed_audio.speech_verified
        ? `đã đối chiếu lời đọc; phần yên có thể xử lý ${selectedRow.dubbed_audio.trim_start_ms} / ${selectedRow.dubbed_audio.trim_end_ms} ms, chưa áp dụng`
        : 'chưa đủ bằng chứng để cắt phần yên'}.</p>}
      {selectedRow.dubbed_audio?.diagnostics?.transcript_matches
        && selectedRow.dubbed_audio.diagnostics.minimum_confidence < .65 && <p>
          ASR nhận đúng chữ nhưng chưa chắc ở từ: {selectedRow.dubbed_audio.diagnostics.low_confidence_words.join(', ')}.
          Chưa có căn cứ kết luận giọng bị thiếu từ hoặc phải tạo lại.</p>}
      {selectedRow.processing_error && <p className="voice-error">{selectedRow.processing_error}</p>}
      {selectedRow.processed && <div>
        <p>{selectedRow.processed.state === 'ready_for_review'
          ? 'Đã tạo WAV và kiểm tra lại lời đọc sau căn thời gian. Chưa áp dụng vào dự án.'
          : 'Đã tạo WAV nhưng kết quả kiểm tra chưa đạt; giữ nguyên giọng trong dự án.'}</p>
        {selectedRow.processed.issues.map((reason, index) => <p key={index}>{reasons[reason] ?? reason}</p>)}
        <p>Thời lượng WAV thực: {(selectedRow.processed.audio.duration_ms / 1000).toFixed(3)} giây.</p>
        {auditId && <SyncCandidateAudio key={`${auditId}:${selectedRow.clip_id}:${selectedRow.processed.audio.id}`}
          auditId={auditId} clipId={selectedRow.clip_id} />}
      </div>}
    </div>}
  </details>;
}
