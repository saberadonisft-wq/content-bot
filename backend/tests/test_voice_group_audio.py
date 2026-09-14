import array
import math
import sys
import wave
from copy import deepcopy

import pytest
from test_voice_sync_audit import setup

from app.services.voiceover import group_audio as module
from app.services.voiceover import sync_candidate
from app.services.voiceover.audio import audio_metadata
from app.services.voiceover.manager import VoiceManager
from app.services.voiceover.repair_pipeline import run_sync_repair
from app.services.voiceover.source_groups import group_candidates
from app.services.voiceover.store import generation_hash, read_json, write_json
from app.services.voiceover.sync_apply import apply_sync_candidates
from app.services.voiceover.sync_audit import audit_path, snapshot_binding


def audio(path, spans):
    samples = array.array('h', [0] * (1600 * 24))
    for lower, upper in spans:
        for index in range(lower * 24, upper * 24):
            samples[index] = 2000 if index % 48 < 24 else -2000
    if sys.byteorder != 'little':
        samples.byteswap()
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        wav.writeframes(samples.tobytes())


@pytest.mark.parametrize('change', ['none', 'word_crosses_boundary', 'no_pause', 'sound_at_cut', 'extra_word', 'uncertain'])
def test_group_splits_only_between_complete_words_and_quiet_guarded_boundaries(tmp_path, change):
    path = tmp_path / 'group.wav'
    audio(path, [(100, 400), (1000, 1300), *([(690, 710)] if change == 'sound_at_cut' else [])])
    words = [{'text': text, 'start_ms': start, 'end_ms': end, 'confidence': .99}
             for text, start, end in [('Xin', 100, 200), ('chào', 200, 400), ('Tạm', 1000, 1150), ('biệt', 1150, 1300)]]
    if change == 'word_crosses_boundary':
        words = [words[0], {**words[1], 'text': 'chào Tạm', 'end_ms': 1150}, words[-1]]
    elif change == 'no_pause':
        words[2]['start_ms'] = 480
    elif change == 'extra_word':
        words.append({'text': 'nhé', 'start_ms': 1400, 'end_ms': 1500, 'confidence': .99})
    elif change == 'uncertain':
        words[0]['confidence'] = .4
    if change == 'none':
        assert module.split_points(path, ['Xin chào,', 'Tạm biệt.'], words) == [0, 700 * 24, 1600 * 24]
    else:
        with pytest.raises(ValueError):
            module.split_points(path, ['Xin chào,', 'Tạm biệt.'], words)


def test_group_wave_is_split_measured_and_applied_atomically_at_each_source_onset(tmp_path, monkeypatch):
    store, doc, subtitles, media = setup(tmp_path)
    first = doc.clips[0]
    first.spoken_text = 'Xin chào,'
    first.end_ms = 1600
    original_meta = read_json(store.path('user', 'assets', first.asset_id))
    original_meta['generation_hash'] = generation_hash(doc, first, 'cpu')
    write_json(store.path('user', 'assets', first.asset_id), original_meta)
    second = first.model_copy(deep=True, update={'id': 'two', 'spoken_text': 'Tạm biệt.',
        'source_cue_ids': ['cue2'], 'start_ms': 1700, 'end_ms': 2300, 'asset_id': 'f' * 64})
    doc.clips.append(second)
    second_meta = {**original_meta, 'id': second.asset_id, 'generation_hash': generation_hash(doc, second, 'cpu')}
    store.path('user', 'assets', second.asset_id, '.wav').write_bytes(store.path('user', 'assets', first.asset_id, '.wav').read_bytes())
    write_json(store.path('user', 'assets', second.asset_id), second_meta)
    doc = store.save_document('user', doc)
    subtitles['segments'].append({**subtitles['segments'][0], 'id': 'cue2', 'start_ms': 1700, 'end_ms': 2300})
    joined = ' '.join(clip.spoken_text for clip in doc.clips)
    group_path = store.path('user', 'assets', 'e' * 64, '.wav')
    audio(group_path, [(100, 400), (1000, 1300)])
    prototype = doc.clips[0].model_copy(update={'spoken_text': joined})
    meta = {**audio_metadata(group_path), 'id': 'e' * 64, 'device': 'cpu',
            'generation_hash': generation_hash(doc, prototype, 'cpu')}
    write_json(store.path('user', 'assets', meta['id']), meta)
    decision = {'clip_ids': ['one', 'two'], 'source_is_clear': True, 'same_speaking_turn': True,
                'fragmented_clauses': True, 'confidence': .99, 'tail_pause_has_no_semantic_function': False,
                'no_scene_or_speaker_change_at_tail': False, 'reason': 'fixture'}
    rows = [{'clip_id': clip_id, 'completed': True, 'source_evidence': {'method': 'asr_observed',
             'transcript_complete': True, 'start_ms': start, 'end_ms': end}}
            for clip_id, start, end in [('one', 1100, 1500), ('two', 1800, 2200)]]
    assert len(group_candidates(doc, subtitles, {'rows': rows})) == 1
    locked = deepcopy(doc)
    locked.clips[1].sync.text_locked = True
    assert not group_candidates(locked, subtitles, {'rows': rows})

    def inspect(path, text, **kwargs):
        with wave.open(str(path), 'rb') as wav:
            rate, frames = wav.getframerate(), wav.getnframes()
            samples = array.array('h', wav.readframes(frames))
            if sys.byteorder != 'little':
                samples.byteswap()
        audible = [index for index, value in enumerate(samples) if abs(value) > 100]
        start, end = audible[0] * 1000 / rate, (audible[-1] + 1) * 1000 / rate
        duration = frames * 1000 / rate
        words = [{'text': value, 'start_ms': lower, 'end_ms': upper, 'confidence': .99}
            for value, lower, upper in [('Xin', 100, 200), ('chào', 200, 400), ('Tạm', 1000, 1150), ('biệt', 1150, 1300)]] if text == joined else []
        return {'speech_verified': True, 'speech_evidence': {'start_ms': start, 'end_ms': end},
                'issues': [], 'words': words, 'automatic_trim_allowed': True,
                'trim_start_ms': max(0, math.floor(start - 80)), 'trim_end_ms': max(0, math.floor(duration - end - 80))}

    monkeypatch.setattr(module, 'inspect_dubbed_speech', inspect)
    monkeypatch.setattr(sync_candidate, 'inspect_dubbed_speech', inspect)
    project = store.path('user', 'projects', doc.project_id)
    before = project.read_bytes()
    identifier = 'b' * 32
    write_json(audit_path(store, 'user', identifier), {'id': identifier, 'project_id': doc.project_id,
        'state': 'succeeded', 'clip_ids': ['one', 'two'], 'rows': rows, 'warnings': [],
        'source_fingerprint': media['fingerprint'], 'input_binding': snapshot_binding(doc, subtitles)})
    manager = VoiceManager(store)

    def context(service, budget, video, media, items, **kwargs):
        receipt = budget.reserve('gemini', [item['id'] for item in items])
        budget.settle(receipt, succeeded=True)
        return {'decision': decision}

    def generate(manager, owner, voice, budget, candidates, **kwargs):
        assert candidates[0]['spoken_text'] == joined and candidates[0]['member_ids'] == ['one', 'two']
        receipt = budget.reserve('tts', candidates[0]['member_ids'])
        budget.settle(receipt, succeeded=True)
        return [{**candidates[0], 'state': 'generated', 'asset': meta, 'attempt_id': 'e' * 64}]

    def inspect_worker(store, owner, identifier, video, media, voice, subtitles, clip_ids, **kwargs):
        return module.run_group_audio(store, owner=owner, identifier=identifier, media=media, voice=voice,
            subtitles=subtitles, clip_ids=clip_ids, model_dir=kwargs['model_dir'],
            dubbed_whisper_model='small', repair=kwargs['repair'])

    try:
        result = run_sync_repair(manager, None, 'user', identifier, tmp_path / 'unused', media, subtitles,
            device='cpu', model_dir=tmp_path, context=context, generate=generate, inspect=inspect_worker)
        assert result['repair']['budget']['tts_used'] == 2  # Conservative per-member budget, one joint inference.
        assert set(result['repair']['solutions']) == {'one', 'two'}
        identifier = result['sync_audit_id']
    finally:
        manager.shutdown()
    record = read_json(audit_path(store, 'user', identifier))
    assert record['state'] == 'succeeded' and project.read_bytes() == before
    with pytest.raises(ValueError, match='đủ các vế'):
        apply_sync_candidates(store, 'user', identifier, project_id=doc.project_id, revision=doc.revision,
                              subtitles=subtitles, clip_ids=['one'], media=media)
    assert project.read_bytes() == before
    applied = apply_sync_candidates(store, 'user', identifier, project_id=doc.project_id, revision=doc.revision,
                                     subtitles=subtitles, clip_ids=['one', 'two'], media=media)
    assert all(clip.sync.state == 'aligned' for clip in applied.clips)
    assert [clip.spoken_text for clip in applied.clips] == [clip.spoken_text for clip in doc.clips]
    assert [clip.start_ms + clip.offset_ms + clip.sync.alignment.speech_head_ms for clip in applied.clips] == [1100, 1800]
