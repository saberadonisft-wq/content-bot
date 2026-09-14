export type VoiceProfile = {
  id: string; name: string; revision: number; preset: string | null; reference_id: string | null;
  model_id: 'pnnbao-ump/VieNeu-TTS-v3-Turbo' | 'pnnbao-ump/VieNeu-TTS-v2-Turbo'; model_revision: string;
  denoise?: boolean;
};
export type VoiceAlignment = {
  proof_id: string; original_asset_id: string; clip_signature: string; source_signature: string;
  source_start_ms: number; source_end_ms: number; allowed_end_ms: number;
  speech_head_ms: number; speech_tail_ms: number; output_duration_ms: number;
};
export type VoiceSync = {
  timing_origin: 'manual' | 'automatic' | 'legacy_unknown'; timing_locked: boolean; text_locked: boolean;
  state: 'unverified' | 'aligned' | 'needs_review';
  issues: ('source_unverified' | 'missing_audio' | 'starts_early' | 'ends_late' | 'overlap')[];
  alignment?: VoiceAlignment | null;
};
export type VoiceClip = {
  id: string; source_cue_ids: string[]; source_text: string; spoken_text: string;
  start_ms: number; end_ms: number; offset_ms: number; rate: number; gain: number;
  asset_id: string | null; generation_hash: string | null; duration_ms: number;
  status: 'missing' | 'ready' | 'stale' | 'overflow' | 'failed'; error: string | null;
  sync?: VoiceSync;
};
export type VoiceDocument = {
  schema_version: 1 | 2; project_id: string; video_fingerprint: string; revision: number;
  profile: VoiceProfile; clips: VoiceClip[]; pronunciation: Record<string, string>;
  mix: { enabled: boolean; muted: boolean; gain: number; original_gain: number; mode: 'voice' | 'mix' | 'duck' };
};
export type VoiceJob = {
  phase?: string; sync_progress?: number;
  sync_result?: { state: string; audit_id?: string; checked?: number; applied?: number; unverified?: number; message?: string };
  clip_ids?: string[] | null; completed_clip_ids?: string[]; current_clip_id?: string | null;
  id: string; project_id: string; state: 'queued' | 'running' | 'paused' | 'interrupted' | 'canceled' | 'failed' | 'succeeded';
  message: string; completed: number; total: number; failed: { clip_id: string; error: string }[];
  elapsed_seconds: number; eta_seconds: number | null;
};
export type VoiceStatus = { ready: boolean; installed: boolean; message: string; devices: string[];
  presets: { id: string; name: string }[]; model_revision: string | null;
  engines?: { model_id: VoiceProfile['model_id']; model_revision: string; name: string; ready: boolean;
    devices: string[]; presets: { id: string; name: string }[]; setup_command: string; message: string }[] };
export const DEFAULT_PROFILE: VoiceProfile = {
  id: 'ngoc-huyen', name: 'Ngọc Huyền', revision: 1, preset: 'Ngọc Huyền', reference_id: null,
  model_id: 'pnnbao-ump/VieNeu-TTS-v3-Turbo', model_revision: '8b7e9cffb4b41918cb638b9f62f0a751184d14a6',
};
export const voiceClipStart = (clip: VoiceClip) => clip.start_ms + clip.offset_ms;
export const clipSignature = (clip: VoiceClip) => JSON.stringify([clip.id, clip.source_cue_ids, clip.start_ms,
  clip.end_ms, clip.offset_ms, clip.spoken_text, clip.source_text, clip.asset_id, clip.duration_ms]);
export const currentAlignment = (clip: VoiceClip) => {
  const alignment = clip.sync?.alignment;
  return alignment && clip.rate === 1 && alignment.clip_signature === clipSignature(clip) ? alignment : null;
};
export const voiceClipEnd = (clip: VoiceClip) => voiceClipStart(clip) + (currentAlignment(clip)?.output_duration_ms
  ?? (clip.duration_ms ? clip.duration_ms / clip.rate : clip.end_ms - clip.start_ms));
export const VOICE_STATUS_LABELS = { missing: 'Chưa tạo', ready: 'Sẵn sàng', stale: 'Cần tạo lại', overflow: 'Vượt thời lượng', failed: 'Lỗi' };
