import type { VoiceDocument } from './types';
import { refreshTiming } from './timing';

const equal = (a: unknown, b: unknown) => JSON.stringify(a) === JSON.stringify(b);

// Three-way merge: only changes relative to the last observed server document
// are local edits. Never silently replace another editor's newer values.
function mergeValue(base: unknown, local: unknown, remote: unknown, conflict?: 'local' | 'remote'): unknown {
  if (equal(local, base)) return remote;
  if (equal(remote, base) || equal(local, remote)) return local;
  if (base && local && remote && typeof base === 'object' && typeof local === 'object'
    && typeof remote === 'object' && !Array.isArray(base) && !Array.isArray(local) && !Array.isArray(remote)) {
    const b = base as Record<string, unknown>, l = local as Record<string, unknown>, r = remote as Record<string, unknown>;
    return Object.fromEntries([...new Set([...Object.keys(b), ...Object.keys(l), ...Object.keys(r)])]
      .map(key => [key, mergeValue(b[key], l[key], r[key], conflict)]));
  }
  if (conflict) return conflict === 'local' ? local : remote;
  throw new Error('Lời đọc đã được chỉnh ở nơi khác. Bản sửa của bạn vẫn còn trên màn hình; hãy đối chiếu trước khi lưu lại.');
}

export function mergeVoiceDocument(base: VoiceDocument | null, local: VoiceDocument, remote: VoiceDocument | null, conflict?: 'local' | 'remote'): VoiceDocument {
  if (!remote) {
    if (base) throw new Error('Dự án giọng đọc đã bị xóa ở nơi khác.');
    return local;
  }
  if (!base) throw new Error('Dự án giọng đọc đã được tạo ở nơi khác. Hãy tải lại trước khi chỉnh sửa.');
  const byId = (doc: VoiceDocument) => Object.fromEntries(doc.clips.map(clip => [clip.id, clip]));
  const normalized = (doc: VoiceDocument) => ({ ...doc, revision: 0, clips: byId(doc) });
  const merged = mergeValue(normalized(base), normalized(local), normalized(remote), conflict) as Omit<VoiceDocument, 'clips'> & { clips: Record<string, VoiceDocument['clips'][number] | undefined> };
  const result: VoiceDocument = { ...merged, revision: remote.revision,
    clips: Object.values(merged.clips).filter((clip): clip is VoiceDocument['clips'][number] => !!clip)
      .sort((a, b) => a.start_ms - b.start_ms) };
  if (result.clips.some(clip => clip.sync?.alignment)) {
    const generationChanged = !equal([result.profile, result.pronunciation, result.text_normalization ?? 'off'], [remote.profile, remote.pronunciation, remote.text_normalization ?? 'off']);
    result.clips = refreshTiming(result.clips.map(clip => generationChanged && clip.sync?.alignment
      ? { ...clip, sync: { ...clip.sync, alignment: null } } : clip));
  }
  return result;
}
