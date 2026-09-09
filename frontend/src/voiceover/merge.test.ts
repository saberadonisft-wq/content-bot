import { describe, expect, it } from 'vitest';
import { mergeVoiceDocument } from './merge';
import { DEFAULT_PROFILE, type VoiceDocument } from './types';

const fixture = (): VoiceDocument => ({ schema_version: 1, project_id: 'project', video_fingerprint: 'video', revision: 1,
  profile: DEFAULT_PROFILE, pronunciation: {}, mix: { enabled: true, muted: false, gain: 1, original_gain: 1, mode: 'mix' },
  clips: [{ id: 'a', source_cue_ids: ['cue'], source_text: 'Xin chào.', spoken_text: 'Xin chào.', start_ms: 0, end_ms: 3000,
    offset_ms: 0, rate: 1, gain: 1, asset_id: null, generation_hash: null, duration_ms: 0, status: 'missing', error: null }] });

describe('concurrent voice document saves', () => {
  it.each(['local', 'remote'] as const)('resolves only conflicting fields using %s and preserves independent work', choice => {
    const base = fixture(), local = fixture(), remote = fixture();
    local.clips[0].spoken_text = 'Lời trên máy';
    remote.clips[0].spoken_text = 'Lời máy chủ';
    local.mix.gain = .7;
    remote.clips[0].asset_id = 'new-audio';
    remote.revision = 4;
    const merged = mergeVoiceDocument(base, local, remote, choice);
    expect(merged.clips[0].spoken_text).toBe(choice === 'local' ? 'Lời trên máy' : 'Lời máy chủ');
    expect(merged.clips[0].asset_id).toBe('new-audio');
    expect(merged.mix.gain).toBe(.7);
    expect(merged.revision).toBe(4);
    expect(mergeVoiceDocument(remote, merged, remote)).toEqual(merged);
  });
  it('keeps newly generated audio while saving a local timing edit', () => {
    const base = fixture(), local = fixture(), remote = fixture();
    local.clips[0].offset_ms = 500;
    Object.assign(remote.clips[0], { asset_id: 'new-audio', generation_hash: 'hash', duration_ms: 2000, status: 'ready' });
    remote.revision = 2;
    const merged = mergeVoiceDocument(base, local, remote);
    expect(merged.clips[0]).toMatchObject({ asset_id: 'new-audio', offset_ms: 500, duration_ms: 2000 });
    expect(merged.revision).toBe(2);
  });
  it('rejects conflicting text edits without mutating the local draft', () => {
    const base = fixture(), local = fixture(), remote = fixture();
    local.clips[0].spoken_text = 'Bản sửa local'; remote.clips[0].spoken_text = 'Bản sửa khác';
    expect(() => mergeVoiceDocument(base, local, remote)).toThrow('ở nơi khác');
    expect(local.clips[0].spoken_text).toBe('Bản sửa local');
  });
  it('preserves independent changes from another editor', () => {
    const base = fixture(), local = fixture(), remote = fixture();
    local.mix.gain = 0.8; remote.clips[0].spoken_text = 'Lời mới';
    expect(mergeVoiceDocument(base, local, remote)).toMatchObject({ mix: { gain: 0.8 }, clips: [{ spoken_text: 'Lời mới' }] });
  });
  it('does not resurrect a remotely deleted unchanged clip', () => {
    const base = fixture(), local = fixture(), remote = fixture();
    local.mix.gain = 0.8; remote.clips = [];
    expect(mergeVoiceDocument(base, local, remote).clips).toEqual([]);
    local.clips[0].spoken_text = 'Đang sửa';
    expect(() => mergeVoiceDocument(base, local, remote)).toThrow();
  });
  it('rebases typing during an in-flight save onto the committed audio result', () => {
    const submitted = fixture(), typing = fixture(), saved = fixture();
    typing.clips[0].spoken_text = 'Câu vừa nhập khi đang lưu';
    saved.revision = 8;
    saved.clips[0].asset_id = 'worker-output';
    const rebased = mergeVoiceDocument(submitted, typing, saved);
    expect(rebased.clips[0].spoken_text).toBe(typing.clips[0].spoken_text);
    expect(rebased.clips[0].asset_id).toBe('worker-output');
    const following = mergeVoiceDocument(saved, rebased, saved);
    expect(following).toEqual(rebased);
  });
  it('does not overwrite a project created or deleted concurrently', () => {
    expect(() => mergeVoiceDocument(null, fixture(), fixture())).toThrow();
    expect(() => mergeVoiceDocument(fixture(), fixture(), null)).toThrow();
    expect(mergeVoiceDocument(null, fixture(), null)).toEqual(fixture());
  });
});
